#include "vendor/reskate/Engine/Resource/cas_codec.h"
#include "vendor/reskate/Engine/Resource/binary_io.h"
#include <Windows.h>
#include <stdexcept>
#include <map>
#include <mutex>
#include <memory>
namespace dingosdk::frostbite {
namespace {
using Decode = long long(__cdecl*)(const void*,long long,void*,long long,int,int,int,void*,long long,void*,void*,void*,long long,int);
struct Codec {
    HMODULE module{};
    Decode decode{};
    ~Codec() { if(module) FreeLibrary(module); }
};
Decode decoder(const std::filesystem::path& gameRoot) {
    static std::mutex mutex;
    static std::map<std::wstring,std::shared_ptr<Codec>> codecs;
    const auto path=std::filesystem::absolute(gameRoot/L"oo2core_9_win64.dll").lexically_normal();
    std::lock_guard lock(mutex);
    auto found=codecs.find(path.wstring());
    if(found!=codecs.end()) return found->second->decode;
    auto codec=std::make_shared<Codec>();
    codec->module=LoadLibraryExW(path.c_str(),nullptr,LOAD_LIBRARY_SEARCH_DLL_LOAD_DIR|LOAD_LIBRARY_SEARCH_SYSTEM32);
    if(!codec->module) throw std::runtime_error("Unable to load the installed game's Oodle codec");
    codec->decode=reinterpret_cast<Decode>(GetProcAddress(codec->module,"OodleLZ_Decompress"));
    if(!codec->decode) throw std::runtime_error("Oodle decoder export missing");
    codecs.emplace(path.wstring(),codec);
    return codec->decode;
}
}
std::vector<std::byte> decode_cas(std::span<const std::byte> encoded, const CasDecodeOptions& options) {
    // Determine the bounded output size before allocating: large chunk streams
    // no longer repeatedly grow, zero and copy the accumulated buffer.
    BinaryReader sizing(encoded);
    std::size_t total=0;
    while(sizing.remaining()) {
        const auto size=sizing.u32(Endian::big)&0xFFFFFFu;
        const auto code=sizing.u16();
        const auto bytes=sizing.u16(Endian::big)+(std::size_t((code>>8)&15)<<16);
        if(size>options.maximumOutputSize || total>options.maximumOutputSize-size)
            throw std::runtime_error("CAS output exceeds configured limit");
        (void)sizing.view(bytes);total+=size;
    }
    Decode decode{};
    BinaryReader reader(encoded);
    std::vector<std::byte> output;
    output.reserve(total);
    while (reader.remaining()) {
        auto size=reader.u32(Endian::big)&0xFFFFFFu;
        auto code=reader.u16();
        auto bytes=reader.u16(Endian::big) + (std::size_t((code>>8)&15)<<16);
        code &= 0x7f;
        if (size > options.maximumOutputSize || output.size() > options.maximumOutputSize-size)
            throw std::runtime_error("CAS output exceeds configured limit");
        auto data=reader.view(bytes); auto start=output.size(); output.resize(start+size);
        if (code==0) {
            if(bytes!=size) throw std::runtime_error("Raw CAS size mismatch");
            std::memcpy(output.data()+start,data.data(),size);
        } else if(code==0x11 || code==0x15 || code==0x19) {
            if(!decode) decode=decoder(options.gameRoot);
            if(decode(data.data(),bytes,output.data()+start,size,1,0,0,nullptr,0,nullptr,nullptr,nullptr,0,3)!=size)
                throw std::runtime_error("Oodle CAS decode failed");
        } else throw std::runtime_error("Unsupported CAS codec: "+std::to_string(code));
    }
    return output;
}
}
