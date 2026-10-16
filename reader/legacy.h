#pragma once
// Base-game archives of two Plants vs. Zombies titles:
//  - Garden Warfare 2: DbObject superbundle TOCs, bundles stored inline in .sb files
//    (LZ4), Update/Patch delta bundles patched against Data.
//  - Battle for Neighborville: binary TOCs, bundle manifests in .sb files, payloads in
//    install-chunk CAS archives (Oodle), Patch TOCs replacing Data TOCs.
#include "classic_ebx.h"
#include <filesystem>
#include <functional>
#include <memory>
#include <optional>
#include <string>
#include <vector>

namespace legacy {
enum class AssetKind { ebx, resource, chunk };
struct Asset {
    AssetKind kind{};
    std::string name;
    std::array<std::byte, 20> sha1{};
    Guid16 guid{};
    std::uint64_t resourceId{};
    std::uint32_t resourceType{};
    std::vector<std::byte> meta;
    std::uint64_t size{};  // decoded payload size
};
struct LevelInfo {
    std::string key;    // what --level takes
    std::string name;   // a readable title
    std::string superbundle;
};

class Archive {
public:
    virtual ~Archive() = default;
    // Detects the layout under `root`; throws for anything else.
    static std::unique_ptr<Archive> open(const std::filesystem::path& root);
    virtual std::string game() const = 0;
    virtual std::vector<LevelInfo> levels() const = 0;
    // Calls `visit` for each asset of the bundles that make up `level`. `fetch` decodes the payload.
    using Fetch = std::function<std::vector<std::byte>()>;
    virtual void each_asset(const std::string& level,
        const std::function<void(const std::string& bundle, const Asset&, const Fetch&)>& visit) = 0;
    // A chunk listed in a superbundle TOC rather than a bundle.
    virtual std::optional<std::vector<std::byte>> toc_chunk(const Guid16& guid) = 0;
    const std::filesystem::path& root() const { return root_; }
protected:
    std::filesystem::path root_;
};
}
