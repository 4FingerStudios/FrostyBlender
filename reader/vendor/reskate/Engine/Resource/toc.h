#pragma once

// Superbundle TOC reader and patch writer, ported from ReSkateStudio's
// sdk/frostbite/toc. The writer rebuilds the bundle and chunk perfect hashes,
// which is what makes a merged TOC resolvable by the engine.

#include "binary_bundle.h"

#include <cstddef>
#include <cstdint>
#include <span>
#include <string>
#include <vector>

namespace dingosdk::frostbite {

inline constexpr std::size_t tocEnvelopeSize = 0x22C;

struct TocBundle {
    std::string name;
    std::vector<std::byte> region;
    std::uint8_t loadFlag{1};
};

struct TocChunk {
    Guid guid;
    CasIdentifier location;
    std::uint32_t offset{};
    std::uint32_t size{};
    bool removed{};
};

struct TocDocument {
    std::uint32_t flags{};
    std::vector<TocBundle> bundles;
    std::vector<TocChunk> chunks;
    std::vector<std::int32_t> bundleHashMap;
    std::vector<std::int32_t> chunkHashMap;
};

[[nodiscard]] std::uint32_t toc_hash(std::span<const std::byte> key,
                                     std::uint32_t seed = 0x811C9DC5U) noexcept;

// Emits a complete patch TOC, envelope included, with uncompressed names.
[[nodiscard]] std::vector<std::byte> write_patch_toc(std::span<const TocBundle> bundles,
                                                     std::span<const TocChunk> chunks = {},
                                                     std::uint32_t flags = 3);
[[nodiscard]] TocDocument read_toc(std::span<const std::byte> data,
                                   std::span<const std::byte> externalBundleData = {});
void verify_toc(const TocDocument& document);

} // namespace dingosdk::frostbite
