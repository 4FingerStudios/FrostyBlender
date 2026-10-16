#include "classic_ebx.h"
#include "vendor/reskate/Engine/Resource/binary_io.h"
#include <fstream>
#include <stdexcept>

namespace legacy {
namespace fb = dingosdk::frostbite;
namespace fs = std::filesystem;
namespace {
enum Type : std::uint8_t {
    inherited = 0x00, structure = 0x02, pointer = 0x03, array = 0x04, cString = 0x07, enumeration = 0x08,
    fileRef = 0x09, boolean = 0x0A, int8 = 0x0B, uint8 = 0x0C, int16 = 0x0D, uint16 = 0x0E, int32 = 0x0F,
    uint32 = 0x10, uint64 = 0x11, int64 = 0x12, float32 = 0x13, float64 = 0x14, guid = 0x15, sha1 = 0x16,
    resourceRef = 0x17
};
constexpr std::uint32_t version2 = 0x0FB2D1CE, version4 = 0x0FB4D1CE;

std::uint32_t keywordHash(std::string_view text) {
    std::uint32_t hash = 5381;
    for (unsigned char c : text) hash = (hash * 33) ^ c;
    return hash;
}
std::vector<std::byte> readFile(const fs::path& path) {
    std::ifstream file(path, std::ios::binary | std::ios::ate);
    if (!file) throw std::runtime_error("Cannot open " + path.string());
    std::vector<std::byte> data(static_cast<std::size_t>(file.tellg()));
    file.seekg(0);
    file.read(reinterpret_cast<char*>(data.data()), static_cast<std::streamsize>(data.size()));
    return data;
}
Guid16 guidAt(std::span<const std::byte> data, std::size_t at) {
    if (at + 16 > data.size()) throw std::out_of_range("EBX GUID out of range");
    Guid16 g; std::copy_n(data.begin() + static_cast<std::ptrdiff_t>(at), 16, g.begin()); return g;
}
template <class T> T scalar(std::span<const std::byte> data, std::size_t at) {
    if (at + sizeof(T) > data.size()) throw std::out_of_range("EBX field out of range");
    T value; std::memcpy(&value, data.data() + at, sizeof(T)); return value;
}

struct Partition {
    std::span<const std::byte> data;
    const std::vector<EbxField>* fields{};
    const std::vector<EbxClass>* classes{};
    std::vector<std::size_t> fileClasses;  // instance class index -> schema class
    std::uint32_t stringsAt{}, dataAt{}, arraysAt{};
    std::vector<std::array<std::uint32_t, 3>> arrays;
    std::size_t objectCount{};

    std::string text(std::uint32_t offset) const {
        if (offset == 0xFFFFFFFFu) return {};
        std::size_t at = stringsAt + std::size_t{offset};
        std::string value;
        while (at < data.size() && data[at] != std::byte{}) value.push_back(static_cast<char>(data[at++]));
        if (at >= data.size()) throw std::out_of_range("EBX string unterminated");
        return value;
    }
    std::size_t size(const EbxField& f) const {
        switch (f.type) {
        case boolean: case int8: case uint8: return 1;
        case int16: case uint16: return 2;
        case int32: case uint32: case float32: case enumeration: case pointer: case array: case cString: case fileRef: return 4;
        case int64: case uint64: case float64: case resourceRef: return 8;
        case guid: return 16;
        case sha1: return 20;
        case structure: return classes->at(static_cast<std::size_t>(f.ref)).size;
        default: throw std::runtime_error("EBX array of unsupported element type " + std::to_string(f.type));
        }
    }
    Json value(const EbxField& f, std::size_t at, unsigned depth) const {
        if (depth > 48) throw std::runtime_error("EBX nesting exceeded");
        switch (f.type) {
        case boolean: return scalar<std::uint8_t>(data, at) != 0;
        case int8: return scalar<std::int8_t>(data, at);
        case uint8: return scalar<std::uint8_t>(data, at);
        case int16: return scalar<std::int16_t>(data, at);
        case uint16: return scalar<std::uint16_t>(data, at);
        case int32: case enumeration: return scalar<std::int32_t>(data, at);
        case uint32: return scalar<std::uint32_t>(data, at);
        case int64: return scalar<std::int64_t>(data, at);
        case uint64: return scalar<std::uint64_t>(data, at);
        case float32: return scalar<float>(data, at);
        case float64: return scalar<double>(data, at);
        case cString: case fileRef: return text(scalar<std::uint32_t>(data, at));
        case guid: return hex(data.subspan(at, 16));
        case sha1: return hex(data.subspan(at, 20));
        case resourceRef: return Json{{"$resource", std::to_string(scalar<std::uint64_t>(data, at))}};
        case pointer: {
            const auto v = scalar<std::uint32_t>(data, at);
            if (v >> 31) return Json{{"$pointer", 2}, {"index", v & 0x7FFFFFFFu}};
            if (!v || v > objectCount) return nullptr;
            return Json{{"$pointer", 1}, {"index", v - 1}};
        }
        case structure: return object(static_cast<std::size_t>(f.ref), at, depth + 1);
        case array: {
            const auto index = scalar<std::uint32_t>(data, at);
            if (index >= arrays.size()) throw std::out_of_range("EBX array index out of range");
            const auto& holder = classes->at(static_cast<std::size_t>(f.ref));
            if (!holder.fieldCount) return Json::array();
            const auto& member = fields->at(holder.fieldStart);
            const auto stride = size(member);
            const std::size_t start = arraysAt + std::size_t{arrays[index][0]}, count = arrays[index][1];
            if (count && (start > data.size() || count > (data.size() - start) / stride)) throw std::out_of_range("EBX array exceeds partition");
            Json result = Json::array();
            for (std::size_t i = 0; i < count; ++i) result.push_back(value(member, start + i * stride, depth + 1));
            return result;
        }
        default: return nullptr;  // type references, boxed values, delegates: not used by the converter
        }
    }
    void fill(std::size_t klass, std::size_t at, Json& out, unsigned depth) const {
        if (depth > 48) throw std::runtime_error("EBX inheritance too deep");
        const auto& c = classes->at(klass);
        for (std::uint32_t i = 0; i < c.fieldCount; ++i) {
            const auto& f = fields->at(c.fieldStart + i);
            if (f.type == inherited) { fill(static_cast<std::size_t>(f.ref), at, out, depth + 1); continue; }
            out[f.name] = value(f, at + f.offset, depth);
        }
    }
    Json object(std::size_t klass, std::size_t at, unsigned depth) const {
        Json out = Json::object();
        out["$type"] = classes->at(klass).name;
        fill(klass, at, out, depth);
        return out;
    }
};
}

std::string hex(std::span<const std::byte> data) {
    static const char* digits = "0123456789abcdef";
    std::string s;
    for (auto b : data) { auto v = std::to_integer<unsigned>(b); s += digits[v >> 4]; s += digits[v & 15]; }
    return s;
}

bool is_classic_ebx(std::span<const std::byte> data) {
    if (data.size() < 4) return false;
    const auto magic = scalar<std::uint32_t>(data, 0);
    return magic == version2 || magic == version4;
}

void TypeLibrary::read(std::span<const std::byte> data, bool patch, const Json& names) {
    fb::BinaryReader r(data);
    if (r.u32() != version4) throw std::runtime_error("Shared type descriptors have an unknown version");
    const auto classCount = r.u16(), fieldCount = r.u16();
    const auto fieldBase = fields.size(), classBase = classes.size();
    const auto& classNames = names["classes"];
    const auto& fieldNames = names["fields"];
    const auto& perClass = names["class_fields"];
    std::vector<std::uint16_t> rawRefs(fieldCount);
    for (std::uint16_t i = 0; i < fieldCount; ++i) {
        EbxField f;
        f.hash = r.u32();
        f.type = static_cast<std::uint8_t>((r.u16() >> 5) & 0x1F);
        rawRefs[i] = r.u16();
        f.offset = r.u32();
        static_cast<void>(r.u32());
        fields.push_back(f);
    }
    std::vector<std::size_t> owner(fieldCount, SIZE_MAX);
    for (std::uint16_t i = 0; i < classCount; ++i) {
        const auto recordAt = r.position();
        const auto guid = guidAt(data, recordAt);
        r.skip(16);
        const auto bodyAt = r.position();
        EbxClass c;
        const auto hash = r.u32();
        const auto fieldIndex = r.i32();
        c.fieldCount = r.u8();
        c.alignment = r.u8();
        c.type = static_cast<std::uint8_t>((r.u16() >> 5) & 0x1F);
        c.size = r.u16();
        static_cast<void>(r.u16());
        const auto index = classes.size();
        const bool reference = patch && std::equal(data.begin() + static_cast<std::ptrdiff_t>(recordAt),
            data.begin() + static_cast<std::ptrdiff_t>(recordAt + 16), data.begin() + static_cast<std::ptrdiff_t>(bodyAt));
        redirect_.push_back(-1);
        if (reference) {
            const auto found = byGuid_.find(guid);
            if (found == byGuid_.end()) throw std::runtime_error("Patch type descriptor names an unknown base class");
            redirect_.back() = static_cast<std::int64_t>(found->second);
            classes.push_back(classes[found->second]);
            continue;
        }
        // The field index is a byte distance back from the class record to its first field.
        const auto first = (static_cast<std::int64_t>(bodyAt) - fieldIndex - 8) / 16;
        if (first < 0 || static_cast<std::size_t>(first) + c.fieldCount > fieldCount) throw std::runtime_error("Type descriptor fields out of range");
        c.fieldStart = static_cast<std::uint32_t>(fieldBase + static_cast<std::size_t>(first));
        for (std::uint32_t k = 0; k < c.fieldCount; ++k) owner[static_cast<std::size_t>(first) + k] = index;
        const auto key = hex(guid);
        c.name = classNames.contains(key) ? classNames[key].get<std::string>() : key;
        classes.push_back(c);
        byGuid_[guid] = index;
    }
    // References are relative to the owning class, counted through both descriptor files.
    for (std::uint16_t i = 0; i < fieldCount; ++i) {
        auto& f = fields[fieldBase + i];
        if (owner[i] == SIZE_MAX) continue;
        const auto& cname = classes[owner[i]].name;
        const auto hashKey = std::to_string(f.hash);
        if (perClass.contains(cname) && perClass[cname].contains(hashKey)) f.name = perClass[cname][hashKey].get<std::string>();
        else if (fieldNames.contains(hashKey)) f.name = fieldNames[hashKey].get<std::string>();
        else { char buffer[16]; std::snprintf(buffer, sizeof buffer, "%08x", f.hash); f.name = buffer; }
        f.ref = static_cast<std::int32_t>(owner[i]) + static_cast<std::int16_t>(rawRefs[i]);
    }
    static_cast<void>(classBase);
}

void TypeLibrary::load(const fs::path& base, const fs::path& patch, const Json& names) {
    read(readFile(base), false, names);
    if (fs::exists(patch)) read(readFile(patch), true, names);
    for (auto& f : fields) {
        if (f.ref < 0 || static_cast<std::size_t>(f.ref) >= classes.size()) { f.ref = -1; continue; }
        if (redirect_[static_cast<std::size_t>(f.ref)] >= 0) f.ref = static_cast<std::int32_t>(redirect_[static_cast<std::size_t>(f.ref)]);
    }
}

const EbxClass* TypeLibrary::find(const Guid16& guid, std::size_t& index) const {
    const auto found = byGuid_.find(guid);
    if (found == byGuid_.end()) return nullptr;
    index = found->second;
    return &classes[index];
}

Json read_classic_ebx(std::span<const std::byte> data, const TypeLibrary* types) {
    fb::BinaryReader r(data);
    const auto magic = r.u32();
    if (magic != version2 && magic != version4) throw std::runtime_error("Not a classic EBX partition");
    const auto stringsAt = r.u32();
    static_cast<void>(r.u32());
    const auto importCount = r.u32();
    const auto instanceCount = r.u16(), exportedCount = r.u16();
    static_cast<void>(r.u16());
    const auto classCount = r.u16(), fieldCount = r.u16(), namesLength = r.u16();
    const auto stringsLength = r.u32(), arrayCount = r.u32(), dataLength = r.u32();
    const auto fileGuid = guidAt(data, r.position());
    r.seek(0x40);
    Json doc{{"guid", hex(fileGuid)}, {"root_type", ""}, {"objects", Json::array()}, {"imports", Json::array()}};
    for (std::uint32_t i = 0; i < importCount; ++i) {
        const auto at = r.position();
        doc["imports"].push_back({{"file", hex(data.subspan(at, 16))}, {"object", hex(data.subspan(at + 16, 16))}});
        r.skip(32);
    }
    Partition p;
    p.data = data;
    std::vector<EbxField> localFields;
    std::vector<EbxClass> localClasses;
    if (magic == version2) {
        std::unordered_map<std::uint32_t, std::string> keywords;
        const auto table = r.view(namesLength);
        for (std::size_t at = 0; at < table.size();) {
            std::string word;
            while (at < table.size() && table[at] != std::byte{}) word.push_back(static_cast<char>(table[at++]));
            ++at;
            if (!word.empty()) keywords.emplace(keywordHash(word), word);
        }
        auto name = [&](std::uint32_t hash) {
            const auto found = keywords.find(hash);
            return found == keywords.end() ? std::to_string(hash) : found->second;
        };
        for (std::uint16_t i = 0; i < fieldCount; ++i) {
            EbxField f;
            f.hash = r.u32(); f.name = name(f.hash);
            f.type = static_cast<std::uint8_t>((r.u16() >> 4) & 0x1F);
            f.ref = r.u16(); f.offset = r.u32();
            static_cast<void>(r.u32());
            localFields.push_back(f);
        }
        for (std::uint16_t i = 0; i < classCount; ++i) {
            EbxClass c;
            c.name = name(r.u32());
            c.fieldStart = r.u32(); c.fieldCount = r.u8(); c.alignment = r.u8();
            c.type = static_cast<std::uint8_t>((r.u16() >> 4) & 0x1F);
            c.size = r.u16();
            static_cast<void>(r.u16());
            if (c.fieldStart + c.fieldCount > localFields.size()) throw std::runtime_error("EBX class fields out of range");
            localClasses.push_back(c);
        }
        for (auto& f : localFields) if (f.ref >= static_cast<std::int32_t>(localClasses.size())) f.ref = -1;
        p.fields = &localFields; p.classes = &localClasses;
        for (std::uint16_t i = 0; i < classCount; ++i) p.fileClasses.push_back(i);
    } else {
        if (!types) throw std::runtime_error("EBX version 4 needs the game's shared type descriptors");
        p.fields = &types->fields; p.classes = &types->classes;
        for (std::uint16_t i = 0; i < classCount; ++i) {
            std::size_t index{};
            if (!types->find(guidAt(data, r.position()), index)) throw std::runtime_error("EBX class " + hex(data.subspan(r.position(), 16)) + " is not in the type descriptors");
            p.fileClasses.push_back(index);
            r.skip(16);
        }
    }
    std::vector<std::pair<std::uint16_t, std::uint16_t>> groups(instanceCount);
    for (auto& g : groups) { g.first = r.u16(); g.second = r.u16(); }
    r.seek((r.position() + 15) & ~std::size_t{15});
    p.arrays.resize(arrayCount);
    for (auto& a : p.arrays) { a[0] = r.u32(); a[1] = r.u32(); a[2] = r.u32(); }
    p.stringsAt = stringsAt;
    p.dataAt = stringsAt + stringsLength;
    p.arraysAt = p.dataAt + dataLength;
    if (p.arraysAt > data.size()) throw std::runtime_error("EBX sections exceed the partition");
    for (const auto& g : groups) p.objectCount += g.second;
    std::size_t at = p.dataAt;
    for (std::size_t g = 0; g < groups.size(); ++g) {
        if (groups[g].first >= p.fileClasses.size()) throw std::runtime_error("EBX instance class out of range");
        const auto klass = p.fileClasses[groups[g].first];
        const auto& c = p.classes->at(klass);
        const std::size_t align = std::max<std::size_t>(c.alignment, 1);
        for (std::uint16_t i = 0; i < groups[g].second; ++i) {
            at = (at + align - 1) / align * align;
            std::string instance;
            if (g < exportedCount) { instance = hex(data.subspan(at, 16)); at += 16; }
            // The 8-byte DataContainer header overlaps the instance GUID.
            if (align != 4) at += 8;
            const auto base = at - 8;
            auto obj = p.object(klass, base, 0);
            obj["$guid"] = instance.empty() ? Json(nullptr) : Json(instance);
            doc["objects"].push_back(std::move(obj));
            at = base + c.size;
        }
    }
    if (!doc["objects"].empty()) doc["root_type"] = doc["objects"][0]["$type"];
    return doc;
}
}
