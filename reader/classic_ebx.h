#pragma once
// Pre-RIFF ("classic") EBX partitions: version 2 (0x0FB2D1CE) carries its own type
// descriptors and field names, as in Plants vs. Zombies Garden Warfare 2; version 4
// (0x0FB4D1CE) names its classes by GUID only, as in Battle for Neighborville, and is
// laid out from the game's shared type descriptors.
#include "vendor/json.hpp"
#include <array>
#include <cstddef>
#include <cstdint>
#include <filesystem>
#include <map>
#include <span>
#include <string>
#include <unordered_map>
#include <vector>

namespace legacy {
using Json = nlohmann::json;
using Guid16 = std::array<std::byte, 16>;

struct EbxField {
    std::string name;
    std::uint32_t hash{};
    std::uint8_t type{};    // EbxFieldType ordinal
    std::int32_t ref{};     // class index of a struct, array or base class
    std::uint32_t offset{};
};
struct EbxClass {
    std::string name;
    std::uint32_t fieldStart{}, fieldCount{};
    std::uint8_t alignment{}, type{};
    std::uint32_t size{};
};

// SharedTypeDescriptors.ebx and its patch, with class and field names from a type list.
class TypeLibrary {
public:
    void load(const std::filesystem::path& base, const std::filesystem::path& patch, const Json& names);
    const EbxClass* find(const Guid16& guid, std::size_t& index) const;
    std::vector<EbxField> fields;
    std::vector<EbxClass> classes;
private:
    void read(std::span<const std::byte> data, bool patch, const Json& names);
    std::map<Guid16, std::size_t> byGuid_;
    std::vector<std::int64_t> redirect_;  // patch entries that only name an unchanged base class
};

// The document as the converter reads it: guid, root_type, objects, imports.
Json read_classic_ebx(std::span<const std::byte> data, const TypeLibrary* types);
bool is_classic_ebx(std::span<const std::byte> data);
std::string hex(std::span<const std::byte> data);
}
