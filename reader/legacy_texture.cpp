#include "legacy_texture.h"
#include "vendor/reskate/Engine/Resource/binary_io.h"
#include "bcdec.h"
#include <algorithm>
#include <cmath>
#include <stdexcept>
#include <string>

namespace legacy {
namespace fb = dingosdk::frostbite;
namespace {
enum class Codec { bc1, bc2, bc3, bc4, bc5, bc7, rgba8, r8 };
struct Format { Codec codec; std::uint32_t unit; bool block; };
// The Frostbite 2015-2018 texture format ordinals both games use.
Format format(std::uint32_t ordinal) {
    switch (ordinal) {
    case 6: return {Codec::r8, 1, false};
    case 18: case 19: case 20: case 21: return {Codec::rgba8, 4, false};
    case 54: case 55: case 56: case 57: return {Codec::bc1, 8, true};
    case 58: case 59: return {Codec::bc2, 16, true};
    case 60: case 61: return {Codec::bc3, 16, true};
    case 62: return {Codec::bc4, 8, true};
    case 63: return {Codec::bc5, 16, true};
    case 66: case 67: return {Codec::bc7, 16, true};
    default: throw std::runtime_error("Unsupported texture format " + std::to_string(ordinal));
    }
}
std::size_t mipBytes(const Format& f, std::uint32_t w, std::uint32_t h) {
    if (!f.block) return std::size_t{w} * h * f.unit;
    return std::size_t{std::max(1u, (w + 3) / 4)} * std::max(1u, (h + 3) / 4) * f.unit;
}
}

bool is_legacy_texture(std::span<const std::byte> header, std::span<const std::byte> metadata) {
    if (header.size() != 132 || metadata.size() < 4) return false;
    const auto version = std::to_integer<std::uint32_t>(metadata[0]);
    return version == 11 || version == 12;
}

TextureHeader read_legacy_texture(std::span<const std::byte> header, std::span<const std::byte> metadata) {
    if (!is_legacy_texture(header, metadata)) throw std::runtime_error("Unsupported texture header");
    const bool v12 = std::to_integer<std::uint32_t>(metadata[0]) == 12;
    fb::BinaryReader r(header);
    TextureHeader t;
    r.skip(8);
    t.type = r.u32();
    t.format = r.u32();
    if (v12) r.skip(4);
    static_cast<void>(r.u16());
    t.width = r.u16(); t.height = r.u16(); t.depth = r.u16(); t.slices = r.u16();
    t.mips = r.u8(); t.firstMip = r.u8();
    auto g = r.view(16); std::copy(g.begin(), g.end(), t.chunk.begin());
    for (auto& s : t.mipSizes) s = r.u32();
    t.chunkSize = r.u32();
    if (!t.width || !t.height || t.width > 16384 || t.height > 16384) throw std::runtime_error("Texture dimensions are invalid");
    if (!t.mips || t.mips > t.mipSizes.size()) throw std::runtime_error("Texture mip count is invalid");
    return t;
}

fb::Image decode_legacy_texture(const TextureHeader& t, std::span<const std::byte> chunk, std::uint32_t minimum) {
    if (t.type != 0) throw std::runtime_error("Only 2D textures are decoded");
    const auto f = format(t.format);
    std::uint32_t level = 0, width = t.width, height = t.height;
    std::size_t offset = 0;
    while (level + 1u < t.mips && std::min(width, height) / 2 >= minimum) {
        offset += t.mipSizes[level] ? t.mipSizes[level] : mipBytes(f, width, height);
        width = std::max(1u, width / 2); height = std::max(1u, height / 2); ++level;
    }
    const auto size = mipBytes(f, width, height);
    if (offset > chunk.size() || size > chunk.size() - offset) throw std::runtime_error("Texture chunk is truncated");
    const auto* source = reinterpret_cast<const std::uint8_t*>(chunk.data() + offset);
    fb::Image image{width, height, std::vector<std::uint8_t>(std::size_t{width} * height * 4)};
    if (!f.block) {
        for (std::size_t i = 0; i < std::size_t{width} * height; ++i) {
            auto* p = image.rgba.data() + i * 4;
            if (f.codec == Codec::r8) { p[0] = p[1] = p[2] = source[i]; p[3] = 255; }
            else std::copy_n(source + i * 4, 4, p);
        }
        return image;
    }
    const auto bw = (width + 3) / 4, bh = (height + 3) / 4, pw = bw * 4;
    std::vector<std::uint8_t> padded(std::size_t{pw} * bh * 4 * 4);
    const int pitch = static_cast<int>(pw * 4);
    for (std::uint32_t by = 0; by < bh; ++by) {
        for (std::uint32_t bx = 0; bx < bw; ++bx, source += f.unit) {
            auto* out = padded.data() + (std::size_t{by} * 4 * pw + std::size_t{bx} * 4) * 4;
            switch (f.codec) {
            case Codec::bc1: bcdec_bc1(source, out, pitch); break;
            case Codec::bc2: bcdec_bc2(source, out, pitch); break;
            case Codec::bc3: bcdec_bc3(source, out, pitch); break;
            case Codec::bc7: bcdec_bc7(source, out, pitch); break;
            case Codec::bc4: {
                std::uint8_t grey[16]{}; bcdec_bc4(source, grey, 4);
                for (int y = 0; y < 4; ++y) for (int x = 0; x < 4; ++x) { auto* p = out + y * pitch + x * 4; p[0] = p[1] = p[2] = grey[y * 4 + x]; p[3] = 255; }
                break;
            }
            case Codec::bc5: {
                std::uint8_t rg[32]{}; bcdec_bc5(source, rg, 8);
                for (int y = 0; y < 4; ++y) for (int x = 0; x < 4; ++x) {
                    auto* p = out + y * pitch + x * 4;
                    p[0] = rg[y * 8 + x * 2]; p[1] = rg[y * 8 + x * 2 + 1];
                    const float nx = p[0] / 127.5f - 1, ny = p[1] / 127.5f - 1;
                    p[2] = static_cast<std::uint8_t>(std::lround((std::sqrt(std::max(0.0f, 1 - nx * nx - ny * ny)) * 0.5f + 0.5f) * 255));
                    p[3] = 255;
                }
                break;
            }
            default: break;
            }
        }
    }
    for (std::uint32_t y = 0; y < height; ++y)
        std::copy_n(padded.data() + std::size_t{y} * pw * 4, std::size_t{width} * 4, image.rgba.data() + std::size_t{y} * width * 4);
    return image;
}
}
