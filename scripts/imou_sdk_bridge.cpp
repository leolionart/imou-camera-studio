#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <dlfcn.h>
#include <unistd.h>
#include <string>
#include <vector>
#include <iostream>

typedef int (*CLIENT_Init_t)(void*, long);
typedef void (*CLIENT_Cleanup_t)(void);
typedef long (*CLIENT_Login_t)(const char*, unsigned short, const char*, const char*, void*, int*);
typedef int (*CLIENT_Logout_t)(long);
typedef int (*CLIENT_TransmitInfoForWeb_t)(long, const char*, unsigned int, char*, unsigned int, void*, int);
typedef int (*CLIENT_DHPTZControlEx2_t)(long, int, unsigned int, long, long, long, int, void*);
typedef std::string (*AesDecryptFn)(const char*, long long, const char*);

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

int main(int argc, char** argv) {
    if (argc < 2) {
        fprintf(stderr, "Usage: imou_sdk_bridge <decrypt-smartpss | rpc | ptz> [args...]\n");
        return 2;
    }

    // Redirect stdout to stderr during SDK initialization so stdout remains pure JSON
    int saved_stdout = dup(STDOUT_FILENO);
    dup2(STDERR_FILENO, STDOUT_FILENO);

    chdir("/Applications/SmartPSS.app/Contents/MacOS");
    std::string mode = argv[1];

    if (mode == "decrypt-smartpss") {
        void* hLogic = dlopen("/Applications/SmartPSS.app/Contents/Frameworks/libSPSSLogic.dylib", RTLD_LAZY);
        if (!hLogic) {
            dprintf(saved_stdout, "{\"ok\":false,\"error\":\"dlopen libSPSSLogic failed\"}\n");
            return 1;
        }
        AesDecryptFn fnDec = (AesDecryptFn)dlsym(hLogic, "_ZN9SPSSLogic10AesDecryptEPKcxS1_");
        FILE* fp = fopen("/Applications/SmartPSS.app/Contents/MacOS/Organization/OrganizationDevice.xml", "r");
        if (!fp || !fnDec) {
            dprintf(saved_stdout, "{\"ok\":false,\"error\":\"Cannot read OrganizationDevice.xml\"}\n");
            return 1;
        }
        char buf[16384] = {0};
        fread(buf, 1, sizeof(buf) - 1, fp);
        fclose(fp);
        std::string xml(buf);

        dprintf(saved_stdout, "{\"ok\":true,\"devices\":[");
        size_t pos = 0;
        bool first = true;
        while ((pos = xml.find("<Device ", pos)) != std::string::npos) {
            size_t end = xml.find("/>", pos);
            if (end == std::string::npos) break;
            std::string tag = xml.substr(pos, end - pos);
            pos = end + 2;

            auto get_attr = [&](const std::string& k) -> std::string {
                std::string pat = k + "=\"";
                size_t p = tag.find(pat);
                if (p == std::string::npos) return "";
                p += pat.size();
                size_t e = tag.find("\"", p);
                return (e == std::string::npos) ? "" : tag.substr(p, e - p);
            };

            long long idUnique = atoll(get_attr("idUnique").c_str());
            std::string name = get_attr("name");
            std::string domain = get_attr("domain");
            int port = atoi(get_attr("port").c_str());
            std::string encUser = get_attr("username");
            std::string encPass = get_attr("password");

            std::string decUser = fnDec("UsernameUFNTU0FGRVNFQ1JFVFBDVkFMVUVPTkxZS0VZPQ==", idUnique, encUser.c_str());
            std::string decPass = fnDec("PasswordUFNTU0FGRVNFQ1JFVFBDVkFMVUVPTkxZS0VZPQ==", idUnique, encPass.c_str());

            if (!first) dprintf(saved_stdout, ",");
            first = false;
            dprintf(saved_stdout,
                    "{\"idUnique\":%lld,\"name\":\"%s\",\"ip\":\"%s\",\"port\":%d,\"username\":\"%s\",\"password\":\"%s\"}",
                    idUnique, json_escape(name).c_str(), json_escape(domain).c_str(), port,
                    json_escape(decUser).c_str(), json_escape(decPass).c_str());
        }
        dprintf(saved_stdout, "]}\n");
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

    void* hNet = dlopen("/Applications/SmartPSS.app/Contents/Frameworks/libdhnetsdk.so", RTLD_LAZY);
    if (!hNet) {
        dprintf(saved_stdout, "{\"ok\":false,\"error\":\"dlopen libdhnetsdk.so failed\"}\n");
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
            // Trim trailing whitespace/nulls
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
