AppImage runtime corresponding source and relink materials

source-built-runtime-materials.tar.gz contains the captured runtime and static
library sources, full build recipe, compiler inventory/checksums, link map and
successful build log for our shipped runtime. Runtime SHA256:
5b802620a72759cb29522bf70f7dced40f25dd27882d85956a1eb7ab6ca4e289

Runtime source: AppImage/type2-runtime 8f39b89e2ac31e1640b3d3f7e9a5108e6ce805fa.
All libfuse, squashfuse, musl, zlib, zstd and mimalloc linked objects were built
from the included source archives. GCC compiler support has the Runtime Library
Exception; its package versions and checksums and full licenses are recorded.
No proprietary Photon application code is needed to rebuild/relink the runtime.

Extract the archive and read README.md, setup-offline.sh and build-offline.sh.
The recipe supports FUSE_SOURCE for rebuilding with your modified libfuse.
Compiler tools can be obtained by the recorded APK filenames and official
Alpine3.21 repository URL; APK files are retained by the builder, not shipped.
For fully offline setup, supply those APKs in downloads/apks before setup.
Use the resulting runtime with appimagetool --runtime-file and an extracted
AppDir to replace the AppImage container. See upstream BUILD.md for alternatives.
Full component license texts are in third-party-licenses/appimage-runtime/.
