.PHONY: all bridge app dmg zip install run clean

APP_NAME = Imou Camera Studio.app
DMG_NAME = Imou-Camera-Studio-macOS.dmg
ZIP_NAME = Imou-Camera-Studio-macOS.zip
INSTALL_APP = /Applications/$(APP_NAME)
EXISTING_FW ?= /Applications/Imou Camera Studio.app/Contents/Frameworks
SDK_ARCHIVE = vendor/dahua-netsdk-macos-x86_64.tar.gz

all: bridge

bridge:
	mkdir -p scripts/bin
	/usr/bin/clang++ -arch x86_64 -O2 scripts/imou_sdk_bridge.cpp -o scripts/bin/imou_sdk_bridge

app: bridge
	mkdir -p "dist/$(APP_NAME)/Contents/MacOS" "dist/$(APP_NAME)/Contents/Resources" "dist/$(APP_NAME)/Contents/Frameworks"
	/usr/bin/clang -fobjc-arc -arch arm64 -arch x86_64 -framework Cocoa -framework WebKit -O2 src/ImouCameraStudio.m -o "dist/$(APP_NAME)/Contents/MacOS/ImouCameraStudio"
	cp -R scripts assets "dist/$(APP_NAME)/Contents/Resources/"
	cp src/Info.plist "dist/$(APP_NAME)/Contents/Info.plist"
	@if [ -f "assets/AppIcon.icns" ]; then \
		cp "assets/AppIcon.icns" "dist/$(APP_NAME)/Contents/Resources/AppIcon.icns"; \
	elif [ -f "/Applications/Imou Camera Studio.app/Contents/Resources/AppIcon.icns" ]; then \
		cp "/Applications/Imou Camera Studio.app/Contents/Resources/AppIcon.icns" "dist/$(APP_NAME)/Contents/Resources/AppIcon.icns"; \
	fi
	@if [ -f "$(SDK_ARCHIVE)" ]; then \
		tar -xzf "$(SDK_ARCHIVE)" -C "dist/$(APP_NAME)/Contents/Frameworks/"; \
	elif [ -d "$(EXISTING_FW)" ]; then \
		cp -R "$(EXISTING_FW)/"* "dist/$(APP_NAME)/Contents/Frameworks/"; \
	fi
	codesign --force --deep --sign - "dist/$(APP_NAME)"

dmg: app
	rm -rf dist/dmg-staging "dist/$(DMG_NAME)"
	mkdir -p dist/dmg-staging
	cp -R "dist/$(APP_NAME)" dist/dmg-staging/
	ln -s /Applications dist/dmg-staging/Applications
	hdiutil create -volname "Imou Camera Studio" -srcfolder dist/dmg-staging -ov -format UDZO "dist/$(DMG_NAME)"
	rm -rf dist/dmg-staging
	@echo "✅ Created macOS disk image: dist/$(DMG_NAME)"

zip: app
	rm -f "dist/$(ZIP_NAME)"
	ditto -c -k --sequesterRsrc --keepParent "dist/$(APP_NAME)" "dist/$(ZIP_NAME)"
	@echo "✅ Created macOS zip archive: dist/$(ZIP_NAME)"

install: app
	rm -rf "$(INSTALL_APP)"
	cp -R "dist/$(APP_NAME)" "$(INSTALL_APP)"
	codesign --force --deep --sign - "$(INSTALL_APP)"
	@echo "✅ Installed and signed $(INSTALL_APP)"

run:
	bash scripts/start_studio.sh open

clean:
	rm -rf dist scripts/bin/imou_sdk_bridge scripts/Frameworks
