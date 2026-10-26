#!/bin/zsh
# Build for the iOS simulator. DerivedData lives outside ~/Documents (iCloud xattrs break codesign).
set -e
cd "$(dirname $0)/.."
/opt/homebrew/bin/xcodegen generate -q
xcodebuild -project Sethster.xcodeproj -scheme Sethster -destination "platform=iOS Simulator,name=${SIM:-iPhone 18 Pro}" \
  -derivedDataPath ~/Library/Developer/Xcode/DerivedData/Sethster build 2>&1 | grep -E "error|warning:|BUILD" | grep -v appintentsmetadata | sort -u
