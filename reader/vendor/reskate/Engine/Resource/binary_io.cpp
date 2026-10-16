#include "binary_io.h"

namespace dingosdk::frostbite {

std::string Guid::string() const {
    // Canonical text order swaps the first three fields, matching Studio.
    const std::array<std::byte, 16> canonical{
        bytes[3], bytes[2], bytes[1], bytes[0], bytes[5], bytes[4], bytes[7], bytes[6],
        bytes[8], bytes[9], bytes[10], bytes[11], bytes[12], bytes[13], bytes[14], bytes[15]};
    constexpr char digits[] = "0123456789abcdef";
    std::string result;
    result.reserve(36);
    for (std::size_t index = 0; index < canonical.size(); ++index) {
        if (index == 4 || index == 6 || index == 8 || index == 10) result.push_back('-');
        const auto value = std::to_integer<std::uint8_t>(canonical[index]);
        result.push_back(digits[value >> 4]);
        result.push_back(digits[value & 0x0F]);
    }
    return result;
}

} // namespace dingosdk::frostbite
