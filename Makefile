.PHONY: all bridge app install run clean

APP_NAME = Imou Camera Studio.app
INSTALL_APP = /Applications/$(APP_NAME)
SMARTPSS_FW ?= /Applications/SmartPSS.app/Contents/Frameworks

all: bridge

bridge:
	mkdir -p scripts/bin
	ln -sfn "$(SMARTPSS_FW)" scripts/Frameworks
	/usr/bin/clang++ -arch x86_64 -O2 scripts/imou_sdk_bridge.cpp -o scripts/bin/imou_sdk_bridge

app: bridge
	mkdir -p "dist/$(APP_NAME)/Contents/MacOS" "dist/$(APP_NAME)/Contents/Resources"
	/usr/bin/clang -fobjc-arc -framework Cocoa -framework WebKit -O2 src/ImouCameraStudio.m -o "dist/$(APP_NAME)/Contents/MacOS/ImouCameraStudio"
	cp -R scripts assets "dist/$(APP_NAME)/Contents/Resources/"
	@if [ -f "/Applications/SmartPSS.app/Contents/Resources/SmartPSS.icns" ]; then \
		cp "/Applications/SmartPSS.app/Contents/Resources/SmartPSS.icns" "dist/$(APP_NAME)/Contents/Resources/AppIcon.icns"; \
	fi
	@cat > "dist/$(APP_NAME)/Contents/Info.plist" << 'EOF'
	<?xml version="1.0" encoding="UTF-8"?>
	<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
	<plist version="1.0">
	<dict>
	    <key>CFBundleExecutable</key>
	    <string>ImouCameraStudio</string>
	    <key>CFBundleIconFile</key>
	    <string>AppIcon</string>
	    <key>CFBundleIdentifier</key>
	    <string>studio.naai.imou-camera-studio</string>
	    <key>CFBundleName</key>
	    <string>Imou Camera Studio</string>
	    <key>CFBundlePackageType</key>
	    <string>APPL</string>
	    <key>CFBundleShortVersionString</key>
	    <string>1.0.0</string>
	    <key>LSMinimumSystemVersion</key>
	    <string>14.0</string>
	</dict>
	</plist>
	EOF

install: app
	rm -rf "$(INSTALL_APP)"
	cp -R "dist/$(APP_NAME)" "$(INSTALL_APP)"
	@echo "✅ Installed $(INSTALL_APP)"

run:
	bash scripts/start_studio.sh open

clean:
	rm -rf dist scripts/bin/imou_sdk_bridge scripts/Frameworks
