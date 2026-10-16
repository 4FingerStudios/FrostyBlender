#pragma once

// Minimal big/little-endian reader and writer for the native package formats,
// ported from ReSkateStudio's sdk/io so the runtime can rebuild TOCs and
// bundles without pulling in the editor's asset database.

#include <algorithm>
#include <array>
#include <bit>
#include <cstddef>
#include <cstdint>
#include <cstring>
#include <span>
#include <stdexcept>
#include <string>
#include <vector>

namespace dingosdk::frostbite {

enum class Endian { little, big };

struct Guid {
    std::array<std::byte, 16> bytes{};
    auto operator<=>(const Guid&) const = default;
    [[nodiscard]] std::string string() const;
};

struct Sha1 {
    std::array<std::byte, 20> bytes{};
    auto operator<=>(const Sha1&) const = default;
};

class BinaryReader final {
public:
    explicit BinaryReader(std::span<const std::byte> data) : data_(data) {}

    [[nodiscard]] std::size_t position() const noexcept { return position_; }
    [[nodiscard]] std::size_t size() const noexcept { return data_.size(); }
    [[nodiscard]] std::size_t remaining() const noexcept { return data_.size() - position_; }
    void seek(std::size_t position) {
        if (position > data_.size()) throw std::out_of_range("Native reader seek is out of range");
        position_ = position;
    }

    void skip(std::ptrdiff_t amount) {
        const auto target = static_cast<std::ptrdiff_t>(position_) + amount;
        if (target < 0) throw std::out_of_range("Native reader skipped before the start");
        seek(static_cast<std::size_t>(target));
    }

    [[nodiscard]] std::uint8_t u8() { return std::to_integer<std::uint8_t>(view(1)[0]); }
    [[nodiscard]] std::int8_t i8() { return static_cast<std::int8_t>(u8()); }
    [[nodiscard]] std::uint16_t u16(Endian endian = Endian::little) { return integer<std::uint16_t>(endian); }
    [[nodiscard]] std::int16_t i16(Endian endian = Endian::little) {
        return static_cast<std::int16_t>(integer<std::uint16_t>(endian));
    }
    [[nodiscard]] std::uint32_t u32(Endian endian = Endian::little) { return integer<std::uint32_t>(endian); }
    [[nodiscard]] std::int32_t i32(Endian endian = Endian::little) {
        return static_cast<std::int32_t>(integer<std::uint32_t>(endian));
    }
    [[nodiscard]] std::uint64_t u64(Endian endian = Endian::little) { return integer<std::uint64_t>(endian); }
    [[nodiscard]] std::int64_t i64(Endian endian = Endian::little) {
        return static_cast<std::int64_t>(integer<std::uint64_t>(endian));
    }
    [[nodiscard]] float f32(Endian endian = Endian::little) {
        return std::bit_cast<float>(integer<std::uint32_t>(endian));
    }
    [[nodiscard]] double f64(Endian endian = Endian::little) {
        return std::bit_cast<double>(integer<std::uint64_t>(endian));
    }
    // Stops at the first NUL but always consumes the whole field.
    [[nodiscard]] std::string fixed_string(std::size_t length) {
        const auto raw = view(length);
        std::string value;
        for (const auto byte : raw) {
            const auto ch = std::to_integer<unsigned char>(byte);
            if (!ch) break;
            value.push_back(static_cast<char>(ch));
        }
        return value;
    }
    [[nodiscard]] std::string c_string() {
        std::string value;
        while (true) {
            const auto ch = u8();
            if (!ch) return value;
            value.push_back(static_cast<char>(ch));
        }
    }

    [[nodiscard]] Sha1 sha1() {
        Sha1 value;
        const auto raw = view(value.bytes.size());
        std::copy(raw.begin(), raw.end(), value.bytes.begin());
        return value;
    }
    [[nodiscard]] Guid guid(Endian endian = Endian::little) {
        Guid value;
        const auto raw = view(value.bytes.size());
        // The native little-endian form stores the first three fields swapped.
        if (endian == Endian::big) std::reverse_copy(raw.begin(), raw.end(), value.bytes.begin());
        else std::copy(raw.begin(), raw.end(), value.bytes.begin());
        return value;
    }
    [[nodiscard]] std::span<const std::byte> view(std::size_t length) {
        if (length > remaining()) throw std::out_of_range("Native reader ran past the end");
        const auto result = data_.subspan(position_, length);
        position_ += length;
        return result;
    }
    [[nodiscard]] std::vector<std::byte> bytes(std::size_t length) {
        const auto raw = view(length);
        return {raw.begin(), raw.end()};
    }

private:
    template <typename Type> [[nodiscard]] Type integer(Endian endian) {
        const auto raw = view(sizeof(Type));
        Type value{};
        for (std::size_t index = 0; index < sizeof(Type); ++index) {
            const auto byte = static_cast<Type>(std::to_integer<std::uint8_t>(raw[index]));
            value |= endian == Endian::big
                ? byte << ((sizeof(Type) - 1 - index) * 8)
                : byte << (index * 8);
        }
        return value;
    }

    std::span<const std::byte> data_;
    std::size_t position_{};
};

class BinaryWriter final {
public:
    [[nodiscard]] std::size_t position() const noexcept { return position_; }
    [[nodiscard]] const std::vector<std::byte>& data() const noexcept { return data_; }
    [[nodiscard]] std::vector<std::byte> take() { return std::move(data_); }

    void seek(std::size_t position) {
        if (position > data_.size()) data_.resize(position);
        position_ = position;
    }
    void u8(std::uint8_t value) { write(&value, 1); }
    void u16(std::uint16_t value, Endian endian = Endian::little) { integer(value, endian); }
    void u32(std::uint32_t value, Endian endian = Endian::little) { integer(value, endian); }
    void i32(std::int32_t value, Endian endian = Endian::little) {
        integer(static_cast<std::uint32_t>(value), endian);
    }
    void u64(std::uint64_t value, Endian endian = Endian::little) { integer(value, endian); }
    void sha1(const Sha1& value) { bytes(value.bytes); }
    // Mirrors the reader so a value read back writes to the same bytes.
    void guid(const Guid& value, Endian endian = Endian::little) {
        if (endian == Endian::little) { bytes(value.bytes); return; }
        std::array<std::byte, 16> raw{};
        std::reverse_copy(value.bytes.begin(), value.bytes.end(), raw.begin());
        bytes(raw);
    }
    void bytes(std::span<const std::byte> value) {
        write(value.data(), value.size());
    }

private:
    template <typename Type> void integer(Type value, Endian endian) {
        std::array<std::byte, sizeof(Type)> raw{};
        for (std::size_t index = 0; index < sizeof(Type); ++index) {
            const auto shift = endian == Endian::big ? (sizeof(Type) - 1 - index) * 8 : index * 8;
            raw[index] = static_cast<std::byte>((value >> shift) & 0xFF);
        }
        write(raw.data(), raw.size());
    }
    void write(const void* source, std::size_t length) {
        if (!length) return;
        if (position_ + length > data_.size()) data_.resize(position_ + length);
        std::memcpy(data_.data() + position_, source, length);
        position_ += length;
    }

    std::vector<std::byte> data_;
    std::size_t position_{};
};

} // namespace dingosdk::frostbite
