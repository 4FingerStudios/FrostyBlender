#include "legacy.h"
#include "cas_stream.h"
#include "vendor/reskate/Engine/Resource/binary_io.h"
#include "vendor/reskate/Engine/Resource/cas_codec.h"
#include "vendor/reskate/Engine/Vfs/native_db.h"
#include <algorithm>
#include <cstring>
#include <deque>
#include <fstream>
#include <map>
#include <stdexcept>

namespace legacy {
namespace fs = std::filesystem;
namespace fb = dingosdk::frostbite;
namespace db = dingosdk::native_db;
namespace {
std::string lower(std::string s) { for (auto& c : s) c = static_cast<char>(std::tolower(static_cast<unsigned char>(c))); return s; }

class Files {
public:
    std::vector<std::byte> read(const fs::path& path, std::uint64_t offset, std::uint64_t size) {
        auto& file = open(path);
        std::vector<std::byte> data(static_cast<std::size_t>(size));
        file.clear(); file.seekg(static_cast<std::streamoff>(offset));
        if (!file.read(reinterpret_cast<char*>(data.data()), static_cast<std::streamsize>(size)))
            throw std::runtime_error("Short read from " + path.string());
        return data;
    }
    std::vector<std::byte> all(const fs::path& path) {
        std::ifstream file(path, std::ios::binary | std::ios::ate);
        if (!file) throw std::runtime_error("Cannot open " + path.string());
        std::vector<std::byte> data(static_cast<std::size_t>(file.tellg()));
        file.seekg(0); file.read(reinterpret_cast<char*>(data.data()), static_cast<std::streamsize>(data.size()));
        return data;
    }
private:
    std::ifstream& open(const fs::path& path) {
        auto key = path.wstring();
        auto found = handles_.find(key);
        if (found == handles_.end()) {
            if (handles_.size() > 64) handles_.clear();
            found = handles_.emplace(key, std::ifstream(path, std::ios::binary)).first;
            if (!found->second) { handles_.erase(found); throw std::runtime_error("Cannot open " + path.string()); }
        }
        return found->second;
    }
    std::map<std::wstring, std::ifstream> handles_;
};

// ---- native DB (DbObject) envelopes ----
struct DbFile { std::vector<std::byte> bytes; db::Node root; };
DbFile readDb(Files& files, const fs::path& path) {
    DbFile f{files.all(path), {}};
    if (f.bytes.size() <= db::envelope_size || f.bytes[0] != std::byte{0} || f.bytes[1] != std::byte{0xD1} || f.bytes[2] != std::byte{0xCE})
        throw std::runtime_error("Unrecognized TOC envelope in " + path.string());
    if (f.bytes[3] == std::byte{0}) throw std::runtime_error("Obfuscated TOCs are not supported: " + path.string());
    const auto body = std::span<const unsigned char>(reinterpret_cast<const unsigned char*>(f.bytes.data()), f.bytes.size()).subspan(db::envelope_size);
    f.root = db::read(body, path.filename().string(), nullptr, {.unique_fields = false, .max_entries = 4'000'000});
    return f;
}
std::int64_t integer(const db::Node* n) {
    if (!n) return 0;
    const auto p = n->payload();
    std::int64_t v = 0;
    if (n->type == 8 && p.size() == 4) { std::int32_t x; std::memcpy(&x, p.data(), 4); v = x; }
    else if (n->type == 9 && p.size() == 8) std::memcpy(&v, p.data(), 8);
    else if (n->type == 6) v = n->boolean;
    return v;
}
bool flag(const db::Node& n, const char* key) { auto f = n.field(key); return f && f->boolean; }
std::string text(const db::Node& n, const char* key) { auto f = n.field(key); return f ? f->text : std::string(); }

// ---- binary bundle manifests ----
struct Manifest { std::vector<Asset> assets; };  // ebx, then resources, then chunks
std::string cstring(std::span<const std::byte> d, std::size_t at) {
    std::string s;
    while (at < d.size() && d[at] != std::byte{}) s.push_back(static_cast<char>(d[at++]));
    if (at >= d.size()) throw std::runtime_error("Bundle string unterminated");
    return s;
}
Manifest readManifest(std::span<const std::byte> d) {
    fb::BinaryReader r(d);
    if (r.remaining() < 36) throw std::runtime_error("Bundle manifest truncated");
    static_cast<void>(r.u32(fb::Endian::big));
    const auto magicAt = r.position();
    auto endian = fb::Endian::big;
    auto magic = r.u32(endian) ^ 0x7065636Eu;
    if (magic != 0xED1CEDB8u && magic != 0xED1CEDBBu) {
        r.seek(magicAt); endian = fb::Endian::little; magic = r.u32(endian) ^ 0x7065636Eu;
        if (magic != 0xED1CEDB8u && magic != 0xED1CEDBBu) throw std::runtime_error("Bundle manifest magic is unsupported");
    }
    const auto total = r.u32(endian), ebx = r.u32(endian), res = r.u32(endian), chunks = r.u32(endian);
    const std::size_t strings = std::size_t{r.u32(endian)} + 4;
    static_cast<void>(r.u32(endian)); static_cast<void>(r.u32(endian));
    if (std::uint64_t{ebx} + res + chunks != total || std::uint64_t{total} * 20 > r.remaining()) throw std::runtime_error("Bundle manifest counts are invalid");
    Manifest m; m.assets.resize(total);
    for (auto& a : m.assets) { auto v = r.view(20); std::copy(v.begin(), v.end(), a.sha1.begin()); }
    for (std::uint32_t i = 0; i < ebx + res; ++i) {
        auto& a = m.assets[i];
        a.kind = i < ebx ? AssetKind::ebx : AssetKind::resource;
        const auto name = r.u32(endian);
        a.size = r.u32(endian);
        a.name = cstring(d, strings + name);
    }
    for (std::uint32_t i = 0; i < res; ++i) m.assets[ebx + i].resourceType = r.u32(endian);
    for (std::uint32_t i = 0; i < res; ++i) m.assets[ebx + i].meta = r.bytes(16);
    for (std::uint32_t i = 0; i < res; ++i) m.assets[ebx + i].resourceId = r.u64(endian);
    for (std::uint32_t i = 0; i < chunks; ++i) {
        auto& a = m.assets[ebx + res + i];
        a.kind = AssetKind::chunk;
        auto v = r.view(16); std::copy(v.begin(), v.end(), a.guid.begin());
        const auto logicalOffset = r.u32(endian), logicalSize = r.u32(endian);
        a.size = (logicalOffset & 0xFFFFu) | logicalSize;
        a.name = hex(a.guid);
    }
    return m;
}

// ---- non-CAS payload streams ----
struct Piece { fb::CasBlock block; std::vector<std::byte> decoded; bool ready{}; std::size_t size() const { return ready ? decoded.size() : block.size; } };
std::vector<std::byte> decode(const fb::CasBlock& b, const fs::path& root) { std::vector<std::byte> out(b.size); fb::decode_block(b, out.data(), root); return out; }

class Payload {
public:
    virtual ~Payload() = default;
    virtual bool next(Piece& piece) = 0;
};
class Plain final : public Payload {
public:
    Plain(std::span<const std::byte> data, std::size_t at) : data_(data), at_(at) {}
    bool next(Piece& p) override {
        if (at_ >= data_.size()) return false;
        p.ready = false; p.decoded.clear();
        at_ = fb::block_header(data_, at_, p.block);
        return true;
    }
private:
    std::span<const std::byte> data_; std::size_t at_;
};
// Garden Warfare 2's patch format: instructions that copy, patch, add or skip whole blocks of the base bundle.
class Delta final : public Payload {
public:
    Delta(std::span<const std::byte> base, std::size_t baseAt, std::span<const std::byte> delta, std::size_t at, const fs::path& root)
        : base_(base), bp_(baseAt), delta_(delta), dp_(at), root_(root) {}
    bool next(Piece& p) override {
        while (pending_.empty()) {
            if (dp_ >= delta_.size()) {
                if (bp_ >= base_.size()) return false;
                Piece tail; bp_ = fb::block_header(base_, bp_, tail.block); pending_.push_back(std::move(tail));
                break;
            }
            instruction();
        }
        p = std::move(pending_.front()); pending_.pop_front();
        return true;
    }
private:
    std::uint32_t be32() { fb::BinaryReader r(delta_.subspan(dp_, 4)); dp_ += 4; return r.u32(fb::Endian::big); }
    std::uint16_t be16() { fb::BinaryReader r(delta_.subspan(dp_, 2)); dp_ += 2; return r.u16(fb::Endian::big); }
    void instruction() {
        const auto word = be32();
        const auto type = word >> 28, count = word & 0x0FFFFFFFu;
        switch (type) {
        case 0: for (std::uint32_t i = 0; i < count; ++i) { Piece x; bp_ = fb::block_header(base_, bp_, x.block); pending_.push_back(std::move(x)); } break;
        case 1: {
            fb::CasBlock baseBlock; bp_ = fb::block_header(base_, bp_, baseBlock);
            const auto source = decode(baseBlock, root_);
            Piece out; out.ready = true; std::size_t prev = 0;
            for (std::uint32_t i = 0; i < count; ++i) {
                const auto offset = be16(), skip = be16();
                if (offset < prev || offset > source.size()) throw std::runtime_error("Delta fragment out of order");
                out.decoded.insert(out.decoded.end(), source.begin() + prev, source.begin() + offset);
                fb::CasBlock added; dp_ = fb::block_header(delta_, dp_, added);
                const auto bytes = decode(added, root_);
                out.decoded.insert(out.decoded.end(), bytes.begin(), bytes.end());
                prev = std::size_t{offset} + skip;
            }
            if (prev < source.size()) out.decoded.insert(out.decoded.end(), source.begin() + static_cast<std::ptrdiff_t>(prev), source.end());
            pending_.push_back(std::move(out));
            break;
        }
        case 2: {
            const auto start = dp_;
            const std::size_t expected = std::size_t{be16()} + 1;
            fb::CasBlock baseBlock; bp_ = fb::block_header(base_, bp_, baseBlock);
            const auto source = decode(baseBlock, root_);
            Piece out; out.ready = true; std::size_t prev = 0;
            while (dp_ - start < count) {
                const auto offset = be16();
                const auto skip = std::to_integer<std::uint8_t>(delta_[dp_]), add = std::to_integer<std::uint8_t>(delta_[dp_ + 1]);
                dp_ += 2;
                if (offset < prev || offset > source.size() || dp_ + add > delta_.size()) throw std::runtime_error("Delta patch out of range");
                out.decoded.insert(out.decoded.end(), source.begin() + static_cast<std::ptrdiff_t>(prev), source.begin() + offset);
                out.decoded.insert(out.decoded.end(), delta_.begin() + static_cast<std::ptrdiff_t>(dp_), delta_.begin() + static_cast<std::ptrdiff_t>(dp_ + add));
                dp_ += add; prev = std::size_t{offset} + skip;
            }
            if (prev < source.size()) out.decoded.insert(out.decoded.end(), source.begin() + static_cast<std::ptrdiff_t>(prev), source.end());
            if (out.decoded.size() != expected) throw std::runtime_error("Delta patched block size mismatch");
            pending_.push_back(std::move(out));
            break;
        }
        case 3: for (std::uint32_t i = 0; i < count; ++i) { Piece x; dp_ = fb::block_header(delta_, dp_, x.block); pending_.push_back(std::move(x)); } break;
        case 4: for (std::uint32_t i = 0; i < count; ++i) { fb::CasBlock x; bp_ = fb::block_header(base_, bp_, x); } break;
        default: throw std::runtime_error("Unknown delta instruction " + std::to_string(type));
        }
    }
    std::span<const std::byte> base_; std::size_t bp_;
    std::span<const std::byte> delta_; std::size_t dp_;
    const fs::path& root_;
    std::deque<Piece> pending_;
};
class Sequence {
public:
    Sequence(Payload& p, const fs::path& root) : p_(p), root_(root) {}
    std::vector<std::byte> take(std::size_t n) {
        std::vector<std::byte> out;
        out.swap(carry_);
        while (out.size() < n) {
            Piece piece;
            if (!p_.next(piece)) throw std::runtime_error("Bundle payload ended early");
            auto bytes = piece.ready ? std::move(piece.decoded) : decode(piece.block, root_);
            out.insert(out.end(), bytes.begin(), bytes.end());
        }
        if (out.size() > n) { carry_.assign(out.begin() + static_cast<std::ptrdiff_t>(n), out.end()); out.resize(n); }
        return out;
    }
    void skip(std::size_t n) {
        if (carry_.size() >= n) { carry_.erase(carry_.begin(), carry_.begin() + static_cast<std::ptrdiff_t>(n)); return; }
        n -= carry_.size(); carry_.clear();
        while (n) {
            Piece piece;
            if (!p_.next(piece)) throw std::runtime_error("Bundle payload ended early");
            if (piece.size() <= n) { n -= piece.size(); continue; }
            auto bytes = piece.ready ? std::move(piece.decoded) : decode(piece.block, root_);
            carry_.assign(bytes.begin() + static_cast<std::ptrdiff_t>(n), bytes.end());
            n = 0;
        }
    }
private:
    Payload& p_; const fs::path& root_; std::vector<std::byte> carry_;
};

void walk(const Manifest& m, Sequence& seq, const std::string& bundle,
          const std::function<void(const std::string&, const Asset&, const Archive::Fetch&)>& visit) {
    for (const auto& a : m.assets) {
        bool used = false;
        Archive::Fetch fetch = [&] { if (used) throw std::runtime_error("Payload fetched twice"); used = true; return seq.take(static_cast<std::size_t>(a.size)); };
        visit(bundle, a, fetch);
        if (!used) seq.skip(static_cast<std::size_t>(a.size));
    }
}

// ---------------- Garden Warfare 2 ----------------
class Gw2 final : public Archive {
public:
    explicit Gw2(const fs::path& root) {
        root_ = root;
        data_ = root / "Data";
        patch_ = root / "Update" / "Patch" / "Data";
        for (const auto& dir : {patch_, data_}) {
            if (!fs::exists(dir / "layout.toc")) continue;
            auto layout = readDb(files_, dir / "layout.toc");
            if (auto list = layout.root.field("superBundles"))
                for (const auto& sb : list->children) {
                    auto name = text(sb, "name");
                    if (!name.empty() && std::none_of(sbs_.begin(), sbs_.end(), [&](auto& s) { return lower(s) == lower(name); })) sbs_.push_back(name);
                }
        }
    }
    std::string game() const override { return "gw2"; }
    std::vector<LevelInfo> levels() const override {
        std::vector<LevelInfo> out;
        for (const auto& sb : sbs_) {
            const auto l = lower(sb);
            if (l.rfind("win32/levels/", 0) != 0) continue;
            const auto rest = l.substr(13);
            const auto slash = rest.find('/');
            if (slash == std::string::npos || rest.substr(0, slash) != rest.substr(slash + 1)) continue;
            out.push_back({sb, rest.substr(0, slash), sb});
        }
        return out;
    }
    void each_asset(const std::string& level, const std::function<void(const std::string&, const Asset&, const Fetch&)>& visit) override {
        const auto basePath = toc(data_, level), patchPath = toc(patch_, level);
        std::optional<DbFile> base, patch;
        if (fs::exists(basePath)) base = readDb(files_, basePath);
        if (fs::exists(patchPath)) patch = readDb(files_, patchPath);
        if (!base && !patch) throw std::runtime_error("No superbundle named " + level);
        auto baseSb = basePath; baseSb.replace_extension(".sb");
        auto patchSb = patchPath; patchSb.replace_extension(".sb");
        std::map<std::string, const db::Node*> baseBundles;
        if (base) if (auto list = base->root.field("bundles")) for (const auto& b : list->children) baseBundles[lower(text(b, "id"))] = &b;
        const auto& listed = patch ? *patch : *base;
        auto list = listed.root.field("bundles");
        if (!list) return;
        for (const auto& b : list->children) {
            const auto id = text(b, "id");
            const auto offset = static_cast<std::uint64_t>(integer(b.field("offset"))), size = static_cast<std::uint64_t>(integer(b.field("size")));
            if (patch && flag(b, "delta")) {
                std::vector<std::byte> baseData;
                if (auto found = baseBundles.find(lower(id)); found != baseBundles.end())
                    baseData = files_.read(baseSb, static_cast<std::uint64_t>(integer(found->second->field("offset"))), static_cast<std::uint64_t>(integer(found->second->field("size"))));
                const auto delta = files_.read(patchSb, offset, size);
                patched(id, baseData, delta, visit);
            } else {
                const auto data = files_.read(patch && !flag(b, "base") ? patchSb : baseSb, offset, size);
                fb::BinaryReader r(data);
                const auto manifestSize = std::size_t{r.u32(fb::Endian::big)} + 4;
                const auto m = readManifest(std::span(data).first(manifestSize));
                Plain payload(data, manifestSize);
                Sequence seq(payload, root_);
                walk(m, seq, id, visit);
            }
        }
    }
    std::optional<std::vector<std::byte>> toc_chunk(const Guid16& guid) override {
        if (!chunksLoaded_) loadChunks();
        auto found = chunks_.find(guid);
        if (found == chunks_.end()) return std::nullopt;
        const auto stored = files_.read(found->second.file, found->second.offset, found->second.size);
        return fb::decode_cas(stored, {root_});
    }
private:
    struct Chunk { fs::path file; std::uint64_t offset{}, size{}; };
    static fs::path toc(const fs::path& dir, const std::string& sb) { return dir / (sb + ".toc"); }
    void patched(const std::string& id, const std::vector<std::byte>& base, const std::vector<std::byte>& delta,
                 const std::function<void(const std::string&, const Asset&, const Fetch&)>& visit) {
        fb::BinaryReader r(delta);
        if (r.u32(fb::Endian::big) != 1) throw std::runtime_error("Unknown delta bundle version in " + id);
        static_cast<void>(r.u32(fb::Endian::big));
        const auto section = r.u32(fb::Endian::big);
        static_cast<void>(r.u32(fb::Endian::big));
        const auto end = std::size_t{0x10} + section;
        const auto newSize = r.u32(fb::Endian::big);
        std::size_t bp = base.empty() ? 0 : 4;
        std::vector<std::byte> manifest(4);
        for (int i = 0; i < 4; ++i) manifest[i] = static_cast<std::byte>((newSize >> (24 - 8 * i)) & 0xFF);
        while (r.position() < end) {
            const auto word = r.u32(fb::Endian::big);
            const auto type = word >> 28, count = word & 0x0FFFFFFFu;
            if (type == 0) { if (bp + count > base.size()) throw std::runtime_error("Delta manifest copy out of range"); manifest.insert(manifest.end(), base.begin() + static_cast<std::ptrdiff_t>(bp), base.begin() + static_cast<std::ptrdiff_t>(bp + count)); bp += count; }
            else if (type == 4) bp += count;
            else if (type == 8) { auto v = r.view(count); manifest.insert(manifest.end(), v.begin(), v.end()); }
            else throw std::runtime_error("Unknown delta manifest instruction");
        }
        if (manifest.size() != std::size_t{newSize} + 4) throw std::runtime_error("Delta manifest size mismatch in " + id);
        const auto m = readManifest(manifest);
        std::size_t basePayload = 0;
        if (!base.empty()) { fb::BinaryReader b(base); basePayload = std::size_t{b.u32(fb::Endian::big)} + 4; }
        Delta payload(base, basePayload, delta, end, root_);
        Sequence seq(payload, root_);
        walk(m, seq, id, visit);
    }
    void loadChunks() {
        chunksLoaded_ = true;
        for (const auto& dir : {data_, patch_}) {
            for (const auto& sb : sbs_) {
                const auto path = toc(dir, sb);
                if (!fs::exists(path)) continue;
                auto t = readDb(files_, path);
                auto list = t.root.field("chunks");
                if (!list) continue;
                auto sbFile = path; sbFile.replace_extension(".sb");
                for (const auto& c : list->children) {
                    auto id = c.field("id");
                    if (!id || id->payload().size() != 16) continue;
                    Guid16 g; std::memcpy(g.data(), id->payload().data(), 16);
                    chunks_[g] = {sbFile, static_cast<std::uint64_t>(integer(c.field("offset"))), static_cast<std::uint64_t>(integer(c.field("size")))};
                }
            }
        }
    }
    fs::path data_, patch_;
    std::vector<std::string> sbs_;
    Files files_;
    std::map<Guid16, Chunk> chunks_;
    bool chunksLoaded_ = false;
};

// ---------------- Battle for Neighborville ----------------
struct CasRef { bool patch{}; std::uint32_t installChunk{}; std::uint32_t archive{}; std::uint32_t offset{}, size{}; };
CasRef cas32(std::uint32_t enc) { return {(enc & 0xFF0000u) != 0, (enc >> 8) & 0xFFu, enc & 0xFFu}; }
CasRef cas64(std::uint64_t enc) { return {((enc >> 48) & 0xFFu) != 0, static_cast<std::uint32_t>((enc >> 16) & 0xFFFFFFFFu), static_cast<std::uint32_t>(enc & 0xFFFFu)}; }

class Bfn final : public Archive {
public:
    explicit Bfn(const fs::path& root) {
        root_ = root;
        for (auto [dir, names] : {std::pair{root / "Patch", &patchChunks_}, std::pair{root / "Data", &baseChunks_}}) {
            if (!fs::exists(dir / "layout.toc")) continue;
            auto layout = readDb(files_, dir / "layout.toc");
            if (auto list = layout.root.field("superBundles"))
                for (const auto& sb : list->children) {
                    auto name = text(sb, "name");
                    if (!name.empty() && std::none_of(sbs_.begin(), sbs_.end(), [&](auto& s) { return lower(s) == lower(name); })) sbs_.push_back(name);
                }
            if (auto manifest = layout.root.field("installManifest"))
                if (auto chunks = manifest->field("installChunks"))
                    for (const auto& c : chunks->children) names->push_back(text(c, "name"));
        }
        if (patchChunks_.empty()) patchChunks_ = baseChunks_;
    }
    std::string game() const override { return "bfn"; }
    std::vector<LevelInfo> levels() const override {
        std::vector<LevelInfo> out;
        for (const auto& sb : sbs_) {
            if (lower(sb).find("/loc/") != std::string::npos) continue;
            const auto t = const_cast<Bfn*>(this)->toc(sb);
            if (!t) continue;
            for (const auto& [b, source] : t->bundles) {
                const auto l = lower(b.name);
                if (l.rfind("win32/levels/", 0) != 0) continue;
                const auto rest = l.substr(13);
                const auto slash = rest.find('/');
                if (slash == std::string::npos || rest.substr(0, slash) != rest.substr(slash + 1)) continue;
                out.push_back({"win32/levels/" + rest.substr(0, slash), rest.substr(0, slash), sb});
            }
        }
        return out;
    }
    void each_asset(const std::string& level, const std::function<void(const std::string&, const Asset&, const Fetch&)>& visit) override {
        const auto key = lower(level) + "/";
        const auto leaf = key.substr(key.rfind('/', key.size() - 2) + 1);
        bool any = false;
        for (const auto& sb : sbs_) {
            const auto t = toc(sb);
            if (!t || std::none_of(t->bundles.begin(), t->bundles.end(), [&](auto& b) { return lower(b.first.name) == key + leaf.substr(0, leaf.size() - 1); })) continue;
            any = true;
            for (const auto& [b, source] : t->bundles) {
                if (lower(b.name).rfind(key, 0) != 0) continue;
                const auto region = b.inToc ? std::vector<std::byte>(source->body.begin() + static_cast<std::ptrdiff_t>(b.offset), source->body.begin() + static_cast<std::ptrdiff_t>(b.offset + b.size))
                                            : files_.read(source->sb, b.offset, b.size);
                bundle(b.name, region, visit);
            }
        }
        if (!any) throw std::runtime_error("No bundle named " + level);
    }
    std::optional<std::vector<std::byte>> toc_chunk(const Guid16& guid) override {
        if (!chunksLoaded_) {
            chunksLoaded_ = true;
            for (const auto& sb : sbs_) if (auto t = toc(sb)) for (const auto& [g, ref] : t->chunks) chunks_.emplace(g, ref);
        }
        auto found = chunks_.find(guid);
        if (found == chunks_.end()) return std::nullopt;
        return read(found->second);
    }
private:
    struct TocBundle { std::string name; std::uint64_t offset{}, size{}; bool inToc{}; };
    struct Toc { fs::path sb; std::vector<std::byte> body; std::vector<TocBundle> bundles; std::vector<std::pair<Guid16, CasRef>> chunks; };
    // A Patch TOC lists only what the patch changed; the Data TOC of the same superbundle supplies the rest.
    struct View { std::vector<std::pair<TocBundle, const Toc*>> bundles; std::vector<std::pair<Guid16, CasRef>> chunks; std::vector<std::unique_ptr<Toc>> files; };
    const View* toc(const std::string& sb) {
        const auto key = lower(sb);
        if (auto found = tocs_.find(key); found != tocs_.end()) return found->second.get();
        auto view = std::make_unique<View>();
        std::map<std::string, std::size_t> byName;
        std::map<Guid16, std::size_t> byGuid;
        for (const auto* dir : {"Data", "Patch"}) {
            const auto path = root_ / dir / (sb + ".toc");
            if (!fs::exists(path)) continue;
            view->files.push_back(parseToc(path));
            const auto* t = view->files.back().get();
            for (const auto& b : t->bundles) {
                auto [it, inserted] = byName.emplace(lower(b.name), view->bundles.size());
                if (inserted) view->bundles.emplace_back(b, t); else view->bundles[it->second] = {b, t};
            }
            for (const auto& c : t->chunks) {
                auto [it, inserted] = byGuid.emplace(c.first, view->chunks.size());
                if (inserted) view->chunks.push_back(c); else view->chunks[it->second] = c;
            }
        }
        if (view->files.empty()) view.reset();
        return (tocs_[key] = std::move(view)).get();
    }
    std::unique_ptr<Toc> parseToc(const fs::path& path) {
        auto t = std::make_unique<Toc>();
        t->sb = path; t->sb.replace_extension(".sb");
        auto raw = files_.all(path);
        if (raw.size() < db::envelope_size + 48 || raw[1] != std::byte{0xD1}) throw std::runtime_error("TOC envelope invalid: " + path.string());
        t->body.assign(raw.begin() + db::envelope_size, raw.end());
        const std::span<const std::byte> d(t->body);
        fb::BinaryReader r(d);
        std::uint32_t h[12];
        for (auto& v : h) v = r.u32(fb::Endian::big);
        const auto bundleData = h[1], bundleCount = h[2], chunkGuids = h[4], chunkCount = h[5], names = h[8], chunkData = h[9], words = h[10], flags = h[11];
        if (flags & 4) throw std::runtime_error("Compressed TOC names are not supported: " + path.string());
        for (std::uint32_t i = 0; i < bundleCount; ++i) {
            r.seek(bundleData + 16 * i);
            const auto nameAt = r.u32(fb::Endian::big), sizeFlag = r.u32(fb::Endian::big);
            const auto offset = r.u64(fb::Endian::big);
            t->bundles.push_back({cstring(d, std::size_t{names} + nameAt), offset, sizeFlag & 0x3FFFFFFFu, (sizeFlag >> 30) == 1});
        }
        std::vector<std::uint32_t> table(words);
        r.seek(chunkData);
        for (auto& w : table) w = r.u32(fb::Endian::big);
        for (std::uint32_t i = 0; i < chunkCount; ++i) {
            r.seek(chunkGuids + 20 * i);
            Guid16 g; auto v = r.view(16); std::reverse_copy(v.begin(), v.end(), g.begin());
            const auto index = r.i32(fb::Endian::big);
            if (index == -1) continue;
            const auto kind = static_cast<std::uint32_t>(index) >> 24, w = static_cast<std::uint32_t>(index) & 0xFFFFFFu;
            CasRef ref;
            if (kind == 1 && w + 3 <= table.size()) { ref = cas32(table[w]); ref.offset = table[w + 1]; ref.size = table[w + 2]; }
            else if (kind == 0x80 && w + 4 <= table.size()) { ref = cas64((std::uint64_t{table[w]} << 32) | table[w + 1]); ref.offset = table[w + 2]; ref.size = table[w + 3]; }
            else continue;
            t->chunks.emplace_back(g, ref);
        }
        return t;
    }
    fs::path cas(const CasRef& ref) const {
        const auto& names = ref.patch ? patchChunks_ : baseChunks_;
        if (ref.installChunk >= names.size()) throw std::runtime_error("CAS install chunk out of range");
        char file[32]; std::snprintf(file, sizeof file, "cas_%02u.cas", ref.archive);
        return root_ / (ref.patch ? "Patch" : "Data") / "Win32" / names[ref.installChunk] / file;
    }
    std::vector<std::byte> read(const CasRef& ref) { return fb::decode_cas(files_.read(cas(ref), ref.offset, ref.size), {root_}); }
    void bundle(const std::string& name, const std::vector<std::byte>& region,
                const std::function<void(const std::string&, const Asset&, const Fetch&)>& visit) {
        fb::BinaryReader r(region);
        const auto manifestAt = r.u32(fb::Endian::big), manifestSize = r.u32(fb::Endian::big), flagsAt = r.u32(fb::Endian::big), total = r.u32(fb::Endian::big);
        const auto dataAt = r.u32(fb::Endian::big);
        if (std::uint64_t{manifestAt} + manifestSize > region.size() || std::uint64_t{flagsAt} + total > region.size()) throw std::runtime_error("Bundle region invalid: " + name);
        const auto m = readManifest(std::span(region).subspan(manifestAt, manifestSize));
        if (m.assets.size() != total) throw std::runtime_error("Bundle has fewer payloads than assets: " + name);
        r.seek(dataAt);
        CasRef current; bool have = false;
        for (std::size_t i = 0; i < total; ++i) {
            const auto f = std::to_integer<std::uint8_t>(region[flagsAt + i]);
            if (f == 1) { current = cas32(r.u32(fb::Endian::big)); have = true; }
            else if (f) { current = cas64(r.u64(fb::Endian::big)); have = true; }
            if (!have) throw std::runtime_error("Bundle payload has no CAS archive: " + name);
            current.offset = r.u32(fb::Endian::big); current.size = r.u32(fb::Endian::big);
            const auto ref = current;
            Fetch fetch = [this, ref] { return read(ref); };
            visit(name, m.assets[i], fetch);
        }
    }
    std::vector<std::string> sbs_, baseChunks_, patchChunks_;
    Files files_;
    std::map<std::string, std::unique_ptr<View>> tocs_;
    std::map<Guid16, CasRef> chunks_;
    bool chunksLoaded_ = false;
};
}

std::unique_ptr<Archive> Archive::open(const fs::path& root) {
    for (const auto& dir : {root / "Patch", root / "Data"}) {
        if (!fs::exists(dir / "layout.toc")) continue;
        Files files;
        auto layout = readDb(files, dir / "layout.toc");
        if (layout.root.field("installManifest")) return std::make_unique<Bfn>(root);
    }
    if (fs::exists(root / "Data" / "layout.toc")) return std::make_unique<Gw2>(root);
    throw std::runtime_error("No Frostbite Data/layout.toc under " + root.string());
}
}
