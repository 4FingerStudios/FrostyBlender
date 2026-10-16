#pragma once
// 132-byte texture resource headers of Garden Warfare 2 (version 11) and Battle for
// Neighborville (version 12), and CPU decoding of their pixel chunks.
#include "vendor/reskate/Engine/Resource/texture.h"
#include <array>
#include <cstddef>
#include <cstdint>
#include <span>

namespace legacy {
struct TextureHeader {
    std::uint32_t type{}, format{};
    std::uint16_t width{}, height{}, depth{}, slices{};
    std::uint8_t mips{}, firstMip{};
    std::array<std::byte, 16> chunk{};
    std::array<std::uint32_t, 15> mipSizes{};
    std::uint32_t chunkSize{};
};
bool is_legacy_texture(std::span<const std::byte> header, std::span<const std::byte> metadata);
TextureHeader read_legacy_texture(std::span<const std::byte> header, std::span<const std::byte> metadata);
// The smallest mip at least `minimum` pixels on its shorter side (or mip 0), as straight RGBA.
// Two-channel normal maps get a reconstructed Z in blue.
dingosdk::frostbite::Image decode_legacy_texture(const TextureHeader& header, std::span<const std::byte> chunk, std::uint32_t minimum);
}
