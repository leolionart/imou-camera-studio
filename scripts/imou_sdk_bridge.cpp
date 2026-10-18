#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <dlfcn.h>
#include <unistd.h>
#include <mach-o/dyld.h>
#include <libgen.h>
#include <sys/stat.h>
#include <string>
#include <vector>
#include <iostream>

typedef int (*CLIENT_Init_t)(void*, long);
typedef void (*CLIENT_Cleanup_t)(void);
typedef long (*CLIENT_Login_t)(const char*, unsigned short, const char*, const char*, void*, int*);
typedef int (*CLIENT_Logout_t)(long);
typedef int (*CLIENT_TransmitInfoForWeb_t)(long, const char*, unsigned int, char*, unsigned int, void*, int);
typedef int (*CLIENT_DHPTZControlEx2_t)(long, int, unsigned int, long, long, long, int, void*);

static std::string json_escape(const std::string& s) {
    std::string out;
    for (unsigned char c : s) {
        if (c == '\\') out += "\\\\";
        else if (c == '"') out += "\\\"";
        else if (c == '\n') out += "\\n";
        else if (c == '\r') out += "\\r";
        else if (c == '\t') out += "\\t";
        else if (c >= 32) out += (char)c;
    }
    return out;
}

static bool file_exists(const std::string& p) {
    struct stat st;
    return stat(p.c_str(), &st) == 0;
}

static std::string find_frameworks_dir() {
    char exePath[4096] = {0};
    uint32_t size = sizeof(exePath);
    if (_NSGetExecutablePath(exePath, &size) == 0) {
        char realExe[4096] = {0};
        if (realpath(exePath, realExe)) {
            std::string dir1 = dirname(realExe);           // .../scripts/bin
            std::string dir2 = dirname((char*)dir1.c_str()); // .../scripts
            std::string dir3 = dirname((char*)dir2.c_str()); // .../Resources
            std::string dir4 = dirname((char*)dir3.c_str()); // .../Contents

            std::vector<std::string> candidates = {
                dir4 + "/Frameworks",
                dir2 + "/Frameworks",
                dir3 + "/Frameworks",
                "/Applications/Imou Camera Studio.app/Contents/Frameworks",
            };
            for (const auto& c : candidates) {
                if (file_exists(c + "/libdhnetsdk.so")) {
                    return c;
                }
            }
        }
    }
    return "/Applications/Imou Camera Studio.app/Contents/Frameworks";
}

int main(int argc, char** argv) {
    if (argc < 2) {
        fprintf(stderr, "Usage: imou_sdk_bridge <rpc | ptz> [args...]\n");
        return 2;
    }

    // Redirect stdout to stderr during SDK initialization so stdout remains pure JSON
    int saved_stdout = dup(STDOUT_FILENO);
    dup2(STDERR_FILENO, STDOUT_FILENO);

    std::string mode = argv[1];
    if (mode == "decrypt-smartpss") {
        dprintf(saved_stdout, "{\"ok\":true,\"devices\":[]}\n");
        return 0;
    }

    if (argc < 5) {
        dprintf(saved_stdout, "{\"ok\":false,\"error\":\"Missing ip, port, or username\"}\n");
        return 2;
    }

    const char* ip = argv[2];
    unsigned short port = (unsigned short)atoi(argv[3]);
    const char* user = argv[4];
    const char* pass = getenv("IMOU_CAM_PASSWORD");
    if (!pass) pass = "";

    std::string fwDir = find_frameworks_dir();
    chdir(fwDir.c_str());
    std::string sdkPath = fwDir + "/libdhnetsdk.so";

    void* hNet = dlopen(sdkPath.c_str(), RTLD_LAZY);
    if (!hNet) {
        dprintf(saved_stdout, "{\"ok\":false,\"error\":\"dlopen libdhnetsdk.so failed at %s\"}\n",
                json_escape(sdkPath).c_str());
        return 1;
    }

    CLIENT_Init_t fnInit = (CLIENT_Init_t)dlsym(hNet, "CLIENT_Init");
    CLIENT_Cleanup_t fnCleanup = (CLIENT_Cleanup_t)dlsym(hNet, "CLIENT_Cleanup");
    CLIENT_Login_t fnLogin = (CLIENT_Login_t)dlsym(hNet, "CLIENT_Login");
    CLIENT_Logout_t fnLogout = (CLIENT_Logout_t)dlsym(hNet, "CLIENT_Logout");
    CLIENT_TransmitInfoForWeb_t fnTransmit = (CLIENT_TransmitInfoForWeb_t)dlsym(hNet, "CLIENT_TransmitInfoForWeb");
    CLIENT_DHPTZControlEx2_t fnPTZ = (CLIENT_DHPTZControlEx2_t)dlsym(hNet, "CLIENT_DHPTZControlEx2");

    fnInit(NULL, 0);

    char devInfo[1024] = {0};
    int loginErr = 0;
    long handle = fnLogin(ip, port, user, pass, devInfo, &loginErr);
    if (!handle) {
        fnCleanup();
        dprintf(saved_stdout, "{\"ok\":false,\"login_error\":%d,\"error\":\"Login failed (error code %d)\"}\n", loginErr, loginErr);
        return 1;
    }

    if (mode == "ptz") {
        if (argc < 8) {
            fnLogout(handle);
            fnCleanup();
            dprintf(saved_stdout, "{\"ok\":false,\"error\":\"Usage: ptz <ip> <port> <user> <cmd_id> <speed> <duration_ms>\"}\n");
            return 2;
        }
        unsigned int cmdId = (unsigned int)atoi(argv[5]);
        long speed = atol(argv[6]);
        int durationMs = atoi(argv[7]);
        int ok1 = fnPTZ(handle, 0, cmdId, speed, speed, 0, 0, NULL);
        if (durationMs > 0) {
            usleep(durationMs * 1000);
        }
        int ok2 = fnPTZ(handle, 0, cmdId, speed, speed, 0, 1, NULL);
        fnLogout(handle);
        fnCleanup();
        dprintf(saved_stdout, "{\"ok\":%s,\"start_ok\":%d,\"stop_ok\":%d}\n", (ok1 ? "true" : "false"), ok1, ok2);
        return 0;
    }

    if (mode == "rpc") {
        std::vector<std::string> lines;
        std::string line;
        while (std::getline(std::cin, line)) {
            if (!line.empty()) lines.push_back(line);
        }
        dprintf(saved_stdout, "{\"ok\":true,\"responses\":[");
        std::vector<char> outBuf(65536, 0);
        for (size_t i = 0; i < lines.size(); i++) {
            memset(outBuf.data(), 0, outBuf.size());
            int ok = fnTransmit(handle, lines[i].c_str(), (unsigned int)lines[i].size(),
                                outBuf.data(), (unsigned int)(outBuf.size() - 1), NULL, 5000);
            if (i > 0) dprintf(saved_stdout, ",");
            std::string respStr(outBuf.data());
            while (!respStr.empty() && (respStr.back() == '\n' || respStr.back() == '\r' || respStr.back() == ' ' || respStr.back() == '\0')) {
                respStr.pop_back();
            }
            if (respStr.empty() || respStr[0] != '{') {
                dprintf(saved_stdout, "{\"transmit_ok\":%d,\"raw\":\"%s\"}", ok, json_escape(respStr).c_str());
            } else {
                dprintf(saved_stdout, "{\"transmit_ok\":%d,\"data\":%s}", ok, respStr.c_str());
            }
        }
        dprintf(saved_stdout, "]}\n");
        fnLogout(handle);
        fnCleanup();
        return 0;
    }

    fnLogout(handle);
    fnCleanup();
    dprintf(saved_stdout, "{\"ok\":false,\"error\":\"Unknown mode\"}\n");
    return 2;
}
