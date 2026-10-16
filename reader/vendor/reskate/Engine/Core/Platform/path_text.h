#pragma once

#include <filesystem>
#include <string>

namespace dingosdk {

// A path as narrow text.
//
// std::filesystem::path::string() converts to the process's ANSI code page and
// throws when a character has no mapping there: "No mapping for the Unicode
// character exists in the target multi-byte code page". A folder named in
// Cyrillic or CJK is enough, and so is an invisible one -- a directory called
// U+200E (left-to-right mark) has been seen in the wild. The failure lands far
// from the cause, because the path is usually only being written into a log or
// an error message at the time.
//
// UTF-8 can represent every path Windows allows, so every path that becomes a
// narrow string goes through here instead.
inline std::string path_utf8(const std::filesystem::path& path) {
    const auto text = path.u8string();
    return {text.begin(), text.end()};
}

} // namespace dingosdk
