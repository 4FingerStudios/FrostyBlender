#pragma once

// Bundle region and binary bundle manifest, ported from ReSkateStudio's
// sdk/frostbite/binary_bundle with the editor's asset database trimmed out.

#include "binary_io.h"

#include <cstddef>
#include <cstdint>
#include <span>
#include <string>
#include <vector>

namespace dingosdk::frostbite {

struct CasIdentifier {
    bool patch{};
    std::uint32_t installChunk{};
    std::uint16_t archive{};
    auto operator<=>(const CasIdentifier&) const = default;
};

struct BundleFileInfo {
    CasIdentifier location;
    std::uint32_t offset{};
    std::uint32_t size{};
};

// A bundle's region: where every asset payload lives in cas, plus the inline
// manifest describing the assets themselves.
struct BundleRegion {
    std::vector<BundleFileInfo> files;
    std::vector<std::byte> inlineManifest;
};

enum class AssetKind { ebx, resource, chunk };

struct BundleAsset {
    AssetKind kind{};
    std::string name;
    Sha1 sha1;
    Guid guid;
    std::uint64_t resourceId{};
    std::uint32_t resourceType{};
    std::vector<std::byte> resourceMeta;
    std::uint64_t originalSize{};
    std::uint32_t logicalOffset{};
    std::uint32_t logicalSize{};
};

struct BinaryBundle {
    std::vector<BundleAsset> ebx;
    std::vector<BundleAsset> resources;
    std::vector<BundleAsset> chunks;
    std::vector<std::byte> chunkMetadata;
};

[[nodiscard]] BundleRegion read_bundle_region(std::span<const std::byte> data, std::size_t offset = 0);
[[nodiscard]] std::vector<std::byte> write_bundle_region(std::span<const BundleFileInfo> files,
                                                         std::span<const std::byte> inlineManifest = {});
[[nodiscard]] BinaryBundle read_binary_bundle(std::span<const std::byte> data);
[[nodiscard]] std::vector<std::byte> write_binary_bundle(const BinaryBundle& bundle);

} // namespace dingosdk::frostbite
