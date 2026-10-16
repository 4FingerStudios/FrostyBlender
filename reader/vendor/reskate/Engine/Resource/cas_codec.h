#pragma once

// CAS block stream codec, ported from ReSkateStudio's sdk/compression. The
// runtime links lz4, zstd and miniz directly instead of loading Studio's
// side-by-side DLLs, and takes Oodle from the game's own oo2core module.

#include <cstddef>
#include <cstdint>
#include <filesystem>
#include <span>
#include <vector>

namespace dingosdk::frostbite {

struct CasDecodeOptions {
    std::filesystem::path gameRoot;
    std::size_t maximumOutputSize{256ULL * 1024ULL * 1024ULL};
};

enum class CasCompression : std::uint8_t {
    raw = 0x00,
    kraken = 0x11,
    selkie = 0x15,
    leviathan = 0x19
};

struct CasEncodeOptions {
    std::filesystem::path gameRoot;
    CasCompression compression{CasCompression::kraken};
    int compressionLevel{4};
    std::size_t blockSize{0x10000};
};

[[nodiscard]] std::vector<std::byte> decode_cas(std::span<const std::byte> encoded,
                                                const CasDecodeOptions& options = {});
[[nodiscard]] std::vector<std::byte> encode_cas(std::span<const std::byte> decoded,
                                                const CasEncodeOptions& options = {});

} // namespace dingosdk::frostbite
