#!/bin/zsh
# Stage W8: build an isolated, patched arm64 `webots` executable from the source of the installed release and run it
# from a COPY of the installed application. The installed /Applications/Webots.app is only read, never modified.
#
#   tools/webots_w8/build.sh <work dir> <patch: none | A | AB>
#
# none = the unpatched source built the same way (control: separates build effects from patch effects)
# A  = tools/webots_w8/A-vectorProjection.patch (the shared projection, minimal correction)
# AB = A + tools/webots_w8/B-radar-reference.patch (radar measures range, bearing and radial speed of one point)
#
# Needs: Xcode command line tools, python3 (for aqtinstall), curl, ~3 GB in <work dir>. Downloads Qt 6.5.3 (official
# Qt mirrors via aqtinstall, as Webots' own scripts/install/qt_mac_installer.sh does) and Cyberbotics' pinned macOS
# dependency packages (MD5s from dependencies/Makefile.mac at the same commit). Builds only src/glad, src/wren and the
# arm64 half of src/webots; links against the installed application's own libode/libassimp/libOIS/libpico and Qt.
set -euo pipefail
WORK=${1:?work dir}; PATCH=${2:?none, A or AB}
INSTALLED=/Applications/Webots.app
COMMIT=$(cat $INSTALLED/Contents/Resources/commit.txt)       # 838bb6fd3f56adf46579422651179f215ff570ce for R2025b
HERE=${0:A:h}
SRC=$WORK/webots-src
mkdir -p $WORK

if [[ ! -d $SRC/.git ]]; then   # sparse, blobless fetch of exactly the installed commit
  git init -q $SRC
  git -C $SRC remote add origin https://github.com/cyberbotics/webots.git
  git -C $SRC config core.sparseCheckout true
  printf "src/\ninclude/\ndependencies/\nresources/\nscripts/install/\n" > $SRC/.git/info/sparse-checkout
  git -C $SRC fetch -q --depth 1 --filter=blob:none origin $COMMIT
  git -C $SRC checkout -q FETCH_HEAD
fi
[[ $(git -C $SRC rev-parse HEAD) == $COMMIT ]]
# src/glm and src/stb are submodules (header-only): fetch exactly the commits the Webots tree pins
for sub in glm stb; do
  PIN=$(git -C $SRC ls-tree HEAD src/$sub | awk '{print $3}')
  if [[ ! -d $SRC/src/$sub/.git ]]; then
    URL=$(git -C $SRC show HEAD:.gitmodules | git config -f /dev/stdin --get submodule.src/$sub.url)
    rm -rf $SRC/src/$sub && git init -q $SRC/src/$sub && git -C $SRC/src/$sub remote add origin $URL
    git -C $SRC/src/$sub fetch -q --depth 1 origin $PIN && git -C $SRC/src/$sub checkout -q FETCH_HEAD
  fi
  [[ $(git -C $SRC/src/$sub rev-parse HEAD) == $PIN ]]
done

if [[ ! -d $WORK/Qt/6.5.3/macos ]]; then
  python3 -m venv $WORK/aqtenv && $WORK/aqtenv/bin/pip install -q aqtinstall
  $WORK/aqtenv/bin/aqt install-qt --outputdir $WORK/Qt mac desktop 6.5.3 clang_64 -m qtwebsockets
fi
D=$SRC/dependencies; U=https://cyberbotics.com/files/repository/dependencies/mac/release
for pm in assimp-5.2.3.tar.bz2:3d4a0297ef7636dc00a1ddc62ff062e3 freetype2.tar.bz2:810286fe3f188f7ae7dca18aef0d75a1 \
          libOIS-1.3.tar.bz2:84c5b0c18031bcf13977165ade137782 libpico-1.0.tar.bz2:7354aa3a597b1703671372a1a6b9c2fd; do
  p=${pm%%:*}; m=${pm##*:}
  [[ -f $D/$p ]] || curl -sSfL -o $D/$p $U/$p
  [[ $(md5 -q $D/$p) == $m ]] || { echo "MD5 mismatch: $p"; exit 1; }
done
[[ -d $D/assimp ]] || tar xfm $D/assimp-5.2.3.tar.bz2 -C $D
[[ -d $D/freetype2 ]] || tar xfm $D/freetype2.tar.bz2 -C $D
[[ -d $SRC/include/libOIS ]] || tar xfm $D/libOIS-1.3.tar.bz2 -C $SRC
[[ -d $SRC/include/libpico ]] || tar xfm $D/libpico-1.0.tar.bz2 -C $SRC

# WEBOTS_HOME layout expected by src/webots/Makefile: Qt frameworks (with headers) and moc, the installed runtime libs
mkdir -p $SRC/Contents/Frameworks $SRC/Contents/lib/webots $SRC/bin/qt
for fw in $WORK/Qt/6.5.3/macos/lib/Qt*.framework; do ln -sfn $fw $SRC/Contents/Frameworks/; done
ln -sf $WORK/Qt/6.5.3/macos/libexec/moc $SRC/bin/qt/moc
for lib in libode libassimp libOIS libpico; do cp -f $INSTALLED/Contents/lib/webots/$lib.dylib $SRC/Contents/lib/webots/; done

# patches: reset the two touched files, then apply
git -C $SRC checkout -q -- src/webots/maths/WbAffinePlane.hpp src/webots/nodes/WbRadar.cpp
[[ $PATCH != none ]] && git -C $SRC apply $HERE/A-vectorProjection.patch
[[ $PATCH == AB ]] && git -C $SRC apply $HERE/B-radar-reference.patch
git -C $SRC diff > $WORK/applied-$PATCH.diff

export WEBOTS_HOME=$SRC
make -C $SRC/src/glad release >/dev/null
make -C $SRC/src/wren -j$(sysctl -n hw.ncpu) release >/dev/null
rm -rf $SRC/src/webots/build/release  # clean: per-arch objects do not track header dependencies
# arm64 half only: the release recipe would also build x86_64 and lipo both
make -C $SRC/src/webots -j$(sysctl -n hw.ncpu) MAKECMDGOALS=release build/release/arm64/webots

# run from a copy of the installed application with only the executable replaced (arm64 only, ad-hoc signed)
APP=$WORK/Webots-W8-$PATCH.app
rm -rf $APP && cp -R $INSTALLED $APP
cp $SRC/src/webots/build/release/arm64/webots $APP/Contents/MacOS/webots
codesign --force -s - $APP/Contents/MacOS/webots
# aqtinstall's Qt uses @rpath/QtX.framework install names and the executable's rpath is @loader_path/../.. (the
# application root): link the copy's own Contents/Frameworks/QtX.framework there (no relink, headers too small to
# rewrite every install name). Done after signing: codesign refuses a bundle root with extra entries.
for fw in $APP/Contents/Frameworks/Qt*.framework; do ln -sfn Contents/Frameworks/${fw:t} $APP/${fw:t}; done
$APP/Contents/MacOS/webots --version
shasum -a 256 $APP/Contents/MacOS/webots $WORK/applied-$PATCH.diff
echo "built $APP"
