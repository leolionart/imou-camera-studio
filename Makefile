.PHONY: all bridge app install run clean

APP_NAME = Imou Camera Studio.app
INSTALL_APP = /Applications/$(APP_NAME)
EXISTING_FW ?= /Applications/Imou Camera Studio.app/Contents/Frameworks

all: bridge

bridge:
	mkdir -p scripts/bin
	/usr/bin/clang++ -arch x86_64 -O2 scripts/imou_sdk_bridge.cpp -o scripts/bin/imou_sdk_bridge

app: bridge
	mkdir -p "dist/$(APP_NAME)/Contents/MacOS" "dist/$(APP_NAME)/Contents/Resources" "dist/$(APP_NAME)/Contents/Frameworks"
	/usr/bin/clang -fobjc-arc -framework Cocoa -framework WebKit -O2 src/ImouCameraStudio.m -o "dist/$(APP_NAME)/Contents/MacOS/ImouCameraStudio"
	cp -R scripts assets "dist/$(APP_NAME)/Contents/Resources/"
	cp src/Info.plist "dist/$(APP_NAME)/Contents/Info.plist"
	@if [ -d "$(EXISTING_FW)" ]; then \
		cp -R "$(EXISTING_FW)/"* "dist/$(APP_NAME)/Contents/Frameworks/"; \
	fi
	@if [ -f "/Applications/Imou Camera Studio.app/Contents/Resources/AppIcon.icns" ]; then \
		cp "/Applications/Imou Camera Studio.app/Contents/Resources/AppIcon.icns" "dist/$(APP_NAME)/Contents/Resources/AppIcon.icns"; \
	fi
	codesign --force --deep --sign - "dist/$(APP_NAME)"

install: app
	rm -rf "$(INSTALL_APP)"
	cp -R "dist/$(APP_NAME)" "$(INSTALL_APP)"
	codesign --force --deep --sign - "$(INSTALL_APP)"
	@echo "✅ Installed and signed $(INSTALL_APP)"

run:
	bash scripts/start_studio.sh open

clean:
	rm -rf dist scripts/bin/imou_sdk_bridge scripts/Frameworks
