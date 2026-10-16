Source: https://github.com/Dingo-Shenanigans/ReSkate
Revision: 86aa8d38fb5e76f6fc7be632a4bacec4f54b82c3
Selected archive and resource readers, retained under upstream GPL-3.0.

Local modifications: texture.cpp adds unsigned BC4 decoding for Skate's grayscale
texture resource. Engine/Vfs/game_archives.cpp now uses shared lifetime-managed
overlapped handles, cached file lengths, independent 64-bit read offsets, and
reusable per-thread events for concurrent reads. The selected Engine/Core/Platform/path_text.h remains upstream
code. CAS/Oodle decoding is supplied
by the separate local cas_decode.cpp implementation; the game DLL is not vendored.

Additional local modification: texture.cpp decodes unsigned BC5 for GW2 normal maps. The GW2 profile normalizes its legacy texture headers and maps GW2 format ordinal 63 to this decoder; Skate format ordinal 63 retains its existing BC4 meaning.
