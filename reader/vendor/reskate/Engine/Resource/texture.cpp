#include "texture.h"
#define BCDEC_IMPLEMENTATION
#pragma warning(push, 0)
#include "bcdec.h"
#pragma warning(pop)
#include <algorithm>
#include <stdexcept>

namespace dingosdk::frostbite {
namespace {
enum class Codec { bc1, bc2, bc3, bc4, bc5, bc7, rgba8, bgra8 };
struct Format { Codec codec; std::uint32_t unit; bool block; };

// Frostbite format ordinals (Studio's texture_format table), colour formats only.
Format format(std::uint32_t ordinal) {
    switch (ordinal) {
    case 18: case 20: return {Codec::rgba8, 4, false};
    case 23: case 24: return {Codec::bgra8, 4, false};
    case 54: case 55: case 56: case 57: return {Codec::bc1, 8, true};
    case 58: case 59: return {Codec::bc2, 16, true};
    case 60: case 61: return {Codec::bc3, 16, true};
    case 62: case 63: return {Codec::bc4, 8, true};
    case 64: case 65: return {Codec::bc5, 16, true};
    case 66: case 67: return {Codec::bc7, 16, true};
    default: throw std::runtime_error("Unsupported texture format " + std::to_string(ordinal));
    }
}
std::size_t mip_size(const Format& f, std::uint32_t width, std::uint32_t height) {
    if (!f.block) return std::size_t{width} * height * f.unit;
    return std::size_t{std::max(1u, (width + 3) / 4)} * std::max(1u, (height + 3) / 4) * f.unit;
}
}

TextureHeader read_texture_header(std::span<const std::byte> header, std::span<const std::byte> metadata) {
    if (header.size() != 144 && header.size() != 180) throw std::runtime_error("Unsupported texture header length");
    if (metadata.size() < 8) throw std::runtime_error("Texture resource metadata is truncated");
    BinaryReader meta(metadata);
    const auto version = meta.u32();
    if (version < 11 || version > 13) throw std::runtime_error("Unsupported texture resource version");
    const bool extended = header.size() == 180;
    BinaryReader reader(header);
    TextureHeader result;
    reader.skip(8);
    result.type = reader.u32();
    result.format = static_cast<std::uint32_t>(reader.i32());
    if (version >= 12) reader.skip(4);
    result.flags = reader.u16();
    result.width = reader.u16();
    result.height = reader.u16();
    result.depth = reader.u16();
    result.slices = reader.u16();
    result.mip_count = reader.u8();
    result.first_mip = reader.u8();
    reader.skip(extended ? 8 : 4);
    for (auto& byte : result.chunk.bytes) byte = static_cast<std::byte>(reader.u8());
    for (auto& size : result.mip_sizes) size = reader.u32();
    result.chunk_size = reader.u32();
    if (!result.width || !result.height || result.width > 16384 || result.height > 16384)
        throw std::runtime_error("Texture dimensions are invalid");
    if (!result.mip_count || result.mip_count > result.mip_sizes.size())
        throw std::runtime_error("Texture mip count is invalid");
    return result;
}

Image decode_texture(const TextureHeader& header, std::span<const std::byte> chunk, std::uint32_t minimum) {
    if (header.type != 0) throw std::runtime_error("Only 2D textures are decoded");
    const auto f = format(header.format);
    // Mips are stored largest first; pick the smallest still >= minimum.
    std::uint32_t level = 0, width = header.width, height = header.height;
    std::size_t offset = 0;
    while (level + 1u < header.mip_count && std::min(width, height) / 2 >= minimum) {
        offset += mip_size(f, width, height);
        width = std::max(1u, width / 2);
        height = std::max(1u, height / 2);
        ++level;
    }
    const auto size = mip_size(f, width, height);
    if (offset > chunk.size() || size > chunk.size() - offset) throw std::runtime_error("Texture chunk is truncated");
    const auto* source = reinterpret_cast<const std::uint8_t*>(chunk.data() + offset);

    Image image{width, height, std::vector<std::uint8_t>(std::size_t{width} * height * 4)};
    if (!f.block) {
        std::copy_n(source, image.rgba.size(), image.rgba.begin());
        if (f.codec == Codec::bgra8)
            for (std::size_t i = 0; i < image.rgba.size(); i += 4) std::swap(image.rgba[i], image.rgba[i + 2]);
        return image;
    }
    // Decode whole 4x4 blocks into a padded buffer, then crop.
    const auto blocksWide = (width + 3) / 4, blocksHigh = (height + 3) / 4;
    const auto paddedWidth = blocksWide * 4;
    std::vector<std::uint8_t> padded(std::size_t{paddedWidth} * blocksHigh * 4 * 4);
    const auto pitch = static_cast<int>(paddedWidth * 4);
    for (std::uint32_t by = 0; by < blocksHigh; ++by) {
        for (std::uint32_t bx = 0; bx < blocksWide; ++bx) {
            auto* out = padded.data() + (std::size_t{by} * 4 * paddedWidth + std::size_t{bx} * 4) * 4;
            switch (f.codec) {
            case Codec::bc1: bcdec_bc1(source, out, pitch); break;
            case Codec::bc2: bcdec_bc2(source, out, pitch); break;
            case Codec::bc3: bcdec_bc3(source, out, pitch); break;
            case Codec::bc4: {
                std::uint8_t grey[16]{}; bcdec_bc4(source,grey,4);
                for(int y=0;y<4;++y)for(int x=0;x<4;++x) {
                    auto* pixel=out+y*pitch+x*4;pixel[0]=pixel[1]=pixel[2]=grey[y*4+x];pixel[3]=255;
                }
                break;
            }
            case Codec::bc5: {
                std::uint8_t xy[32]{}; bcdec_bc5(source,xy,8);
                for(int y=0;y<4;++y)for(int x=0;x<4;++x) {
                    auto* pixel=out+y*pitch+x*4;
                    pixel[0]=xy[(y*4+x)*2];pixel[1]=xy[(y*4+x)*2+1];pixel[2]=255;pixel[3]=255;
                }
                break;
            }
            default: bcdec_bc7(source, out, pitch); break;
            }
            source += f.unit;
        }
    }
    for (std::uint32_t y = 0; y < height; ++y)
        std::copy_n(padded.data() + std::size_t{y} * paddedWidth * 4, std::size_t{width} * 4,
                    image.rgba.data() + std::size_t{y} * width * 4);
    return image;
}

Image resize(const Image& image, std::uint32_t width, std::uint32_t height) {
    if (image.width == width && image.height == height) return image;
    Image result{width, height, std::vector<std::uint8_t>(std::size_t{width} * height * 4)};
    for (std::uint32_t y = 0; y < height; ++y) {
        const auto y0 = std::size_t{y} * image.height / height;
        const auto y1 = std::max(y0 + 1, std::size_t{y + 1} * image.height / height);
        for (std::uint32_t x = 0; x < width; ++x) {
            const auto x0 = std::size_t{x} * image.width / width;
            const auto x1 = std::max(x0 + 1, std::size_t{x + 1} * image.width / width);
            // Average in premultiplied alpha so transparent edges do not bleed colour.
            std::uint64_t sum[4]{};
            for (auto sy = y0; sy < y1; ++sy) {
                for (auto sx = x0; sx < x1; ++sx) {
                    const auto* p = image.rgba.data() + (sy * image.width + sx) * 4;
                    for (int c = 0; c < 3; ++c) sum[c] += std::uint64_t{p[c]} * p[3];
                    sum[3] += p[3];
                }
            }
            const auto count = (y1 - y0) * (x1 - x0);
            auto* out = result.rgba.data() + (std::size_t{y} * width + x) * 4;
            out[3] = static_cast<std::uint8_t>(sum[3] / count);
            for (int c = 0; c < 3; ++c) out[c] = static_cast<std::uint8_t>(sum[3] ? sum[c] / sum[3] : 0);
        }
    }
    return result;
}
}
