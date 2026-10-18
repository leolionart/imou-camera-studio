#import <Cocoa/Cocoa.h>
#import <WebKit/WebKit.h>
#include <stdlib.h>

@interface AppDelegate : NSObject <NSApplicationDelegate, WKUIDelegate, NSWindowDelegate>
@property (strong) NSWindow *window;
@property (strong) WKWebView *webView;
@end

@implementation AppDelegate

- (void)applicationDidFinishLaunching:(NSNotification *)notification {
    NSString *bundlePath = [[NSBundle mainBundle] resourcePath];
    NSString *startScript = [bundlePath stringByAppendingPathComponent:@"scripts/start_studio.sh"];
    if ([[NSFileManager defaultManager] fileExistsAtPath:startScript]) {
        NSString *cmd = [NSString stringWithFormat:@"/bin/bash '%@' start >/dev/null 2>&1", startScript];
        system([cmd UTF8String]);
    }

    NSRect frame = NSMakeRect(0, 0, 1340, 860);
    NSUInteger style = NSWindowStyleMaskTitled | NSWindowStyleMaskClosable | NSWindowStyleMaskMiniaturizable | NSWindowStyleMaskResizable;
    self.window = [[NSWindow alloc] initWithContentRect:frame styleMask:style backing:NSBackingStoreBuffered defer:NO];
    [self.window setTitle:@"Imou Camera Studio — SmartPSS for macOS"];
    [self.window center];
    [self.window setDelegate:self];
    [self.window setBackgroundColor:[NSColor colorWithSRGBRed:0.043 green:0.059 blue:0.098 alpha:1.0]];

    WKWebViewConfiguration *config = [[WKWebViewConfiguration alloc] init];
    self.webView = [[WKWebView alloc] initWithFrame:[[self.window contentView] bounds] configuration:config];
    [self.webView setAutoresizingMask:(NSViewWidthSizable | NSViewHeightSizable)];
    [self.webView setUIDelegate:self];
    [[self.window contentView] addSubview:self.webView];

    NSURL *url = [NSURL URLWithString:@"http://127.0.0.1:8766"];
    [self.webView loadRequest:[NSURLRequest requestWithURL:url]];

    [self.window makeKeyAndOrderFront:nil];
    [NSApp activateIgnoringOtherApps:YES];
}

- (BOOL)applicationShouldTerminateAfterLastWindowClosed:(NSApplication *)sender {
    return YES;
}

- (void)webView:(WKWebView *)webView runJavaScriptAlertPanelWithMessage:(NSString *)message initiatedByFrame:(WKFrameInfo *)frame completionHandler:(void (^)(void))completionHandler {
    NSAlert *alert = [[NSAlert alloc] init];
    [alert setMessageText:@"Imou Camera Studio"];
    [alert setInformativeText:message];
    [alert addButtonWithTitle:@"OK"];
    [alert runModal];
    completionHandler();
}

- (void)webView:(WKWebView *)webView runJavaScriptConfirmPanelWithMessage:(NSString *)message initiatedByFrame:(WKFrameInfo *)frame completionHandler:(void (^)(BOOL))completionHandler {
    NSAlert *alert = [[NSAlert alloc] init];
    [alert setMessageText:@"Xác nhận"];
    [alert setInformativeText:message];
    [alert addButtonWithTitle:@"Đồng ý"];
    [alert addButtonWithTitle:@"Huỷ"];
    NSModalResponse resp = [alert runModal];
    completionHandler(resp == NSAlertFirstButtonReturn);
}

- (void)webView:(WKWebView *)webView runJavaScriptTextInputPanelWithPrompt:(NSString *)prompt defaultText:(NSString *)defaultText initiatedByFrame:(WKFrameInfo *)frame completionHandler:(void (^)(NSString * _Nullable))completionHandler {
    NSAlert *alert = [[NSAlert alloc] init];
    [alert setMessageText:@"Imou Camera Studio"];
    [alert setInformativeText:prompt];
    [alert addButtonWithTitle:@"Lưu"];
    [alert addButtonWithTitle:@"Huỷ"];
    NSTextField *input = [[NSTextField alloc] initWithFrame:NSMakeRect(0, 0, 300, 24)];
    [input setStringValue:(defaultText ? defaultText : @"")];
    [alert setAccessoryView:input];
    [alert.window setInitialFirstResponder:input];
    NSModalResponse resp = [alert runModal];
    if (resp == NSAlertFirstButtonReturn) {
        [input validateEditing];
        completionHandler([input stringValue]);
    } else {
        completionHandler(nil);
    }
}

@end

int main(int argc, const char * argv[]) {
    @autoreleasepool {
        NSApplication *app = [NSApplication sharedApplication];
        [app setActivationPolicy:NSApplicationActivationPolicyRegular];

        NSMenu *menubar = [[NSMenu alloc] init];
        NSMenuItem *appMenuItem = [[NSMenuItem alloc] init];
        [menubar addItem:appMenuItem];
        [app setMainMenu:menubar];
        NSMenu *appMenu = [[NSMenu alloc] init];
        [appMenu addItem:[[NSMenuItem alloc] initWithTitle:@"Quit Imou Camera Studio" action:@selector(terminate:) keyEquivalent:@"q"]];
        [appMenuItem setSubmenu:appMenu];

        NSMenuItem *editMenuItem = [[NSMenuItem alloc] init];
        [menubar addItem:editMenuItem];
        NSMenu *editMenu = [[NSMenu alloc] initWithTitle:@"Edit"];
        [editMenu addItemWithTitle:@"Cut" action:@selector(cut:) keyEquivalent:@"x"];
        [editMenu addItemWithTitle:@"Copy" action:@selector(copy:) keyEquivalent:@"c"];
        [editMenu addItemWithTitle:@"Paste" action:@selector(paste:) keyEquivalent:@"v"];
        [editMenu addItemWithTitle:@"Select All" action:@selector(selectAll:) keyEquivalent:@"a"];
        [editMenuItem setSubmenu:editMenu];

        AppDelegate *delegate = [[AppDelegate alloc] init];
        [app setDelegate:delegate];
        [app run];
    }
    return 0;
}
