#pragma once
// Frostbite texture resources (ported from ReSkateStudio's
// sdk/resources/texture_resource) and CPU decoding of their pixel chunks.
#include "binary_io.h"
#include <array>
#include <cstddef>
#include <cstdint>
#include <span>
#include <vector>

namespace dingosdk::frostbite {
inline constexpr std::uint32_t texture_resource_type = 0x6BDE20BA;

struct TextureHeader {
    std::uint32_t type{};    // 0 = 2D
    std::uint32_t format{};  // Frostbite format ordinal
    std::uint16_t flags{};
    std::uint16_t width{}, height{}, depth{}, slices{};
    std::uint8_t mip_count{}, first_mip{};
    Guid chunk;
    std::array<std::uint32_t, 15> mip_sizes{};
    std::uint32_t chunk_size{};
};
// `header` is the resource payload, `metadata` its bundle resource metadata.
TextureHeader read_texture_header(std::span<const std::byte> header, std::span<const std::byte> metadata);

struct Image {
    std::uint32_t width{}, height{};
    std::vector<std::uint8_t> rgba;  // straight alpha, rows top to bottom
};
// The smallest mip at least `minimum` pixels on its shorter side (or mip 0),
// decoded to RGBA. Supports BC1-3, BC7 and 8-bit RGBA/BGRA; throws otherwise.
Image decode_texture(const TextureHeader& header, std::span<const std::byte> chunk, std::uint32_t minimum);
// Box-filtered resize to exactly `width` x `height`.
Image resize(const Image& image, std::uint32_t width, std::uint32_t height);
}
