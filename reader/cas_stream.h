#pragma once
// Individual compressed blocks of a CAS or non-CAS payload stream. Garden Warfare 2
// stores its bundle payloads back to back in superbundles, so a reader walks them
// block by block and decodes only the assets it wants.
#include <cstddef>
#include <cstdint>
#include <filesystem>
#include <span>

namespace dingosdk::frostbite {
struct CasBlock {
    std::size_t size{};    // decoded bytes
    std::size_t packed{};  // stored bytes
    std::uint8_t codec{};
    std::span<const std::byte> data;
};
// Reads the 8-byte header at `at`; returns the offset of the next block.
std::size_t block_header(std::span<const std::byte> data, std::size_t at, CasBlock& block);
// Decodes into `out`, which must hold `block.size` bytes.
void decode_block(const CasBlock& block, std::byte* out, const std::filesystem::path& gameRoot);
}
