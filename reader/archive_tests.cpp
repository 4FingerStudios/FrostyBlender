// Generated fixtures only. Exercise concurrent reads from one shared CAS handle.
#include "vendor/reskate/Engine/Vfs/game_archives.h"
#include "vendor/reskate/Engine/Resource/cas_codec.h"
#include "vendor/reskate/Engine/Resource/texture.h"
#include <atomic>
#include <chrono>
#include <fstream>
#include <iostream>
#include <thread>
#include <stdexcept>
namespace fs=std::filesystem;
namespace fb=dingosdk::frostbite;
namespace db=dingosdk::native_db;
namespace {
void check(bool passed,const char* message) {if(!passed) throw std::runtime_error(message);}
template<class F> void rejects(F call) {
    bool rejected=false;try {call();} catch(const std::exception&) {rejected=true;}
    check(rejected,"Malformed input was accepted");
}
}
int archiveSelfTest() {
    const auto root=fs::temp_directory_path()/fs::path("aura-archive-test-"+std::to_string(std::chrono::steady_clock::now().time_since_epoch().count()));
    try {
        fb::TextureHeader texture;texture.type=0;texture.format=64;
        texture.width=4;texture.height=4;texture.mip_count=1;
        std::vector<std::byte> bc5(16);bc5[0]=bc5[1]=std::byte{128};bc5[8]=bc5[9]=std::byte{64};
        const auto normal=fb::decode_texture(texture,bc5,1);
        for(size_t pixel=0;pixel<normal.rgba.size();pixel+=4)
            check(normal.rgba[pixel]==128 && normal.rgba[pixel+1]==64 && normal.rgba[pixel+2]==255 && normal.rgba[pixel+3]==255,"GW2 BC5 normal decoding failed");
        texture.format=63;
        const auto grey=fb::decode_texture(texture,std::span<const std::byte>(bc5).first(8),1);
        check(grey.rgba[0]==128 && grey.rgba[1]==128 && grey.rgba[2]==128,"Skate BC4 format meaning changed");
        auto package=root/"Win32/test";fs::create_directories(package);
        std::vector<std::byte> bytes(2*1024*1024);
        for(size_t i=0;i<bytes.size();++i)bytes[i]=std::byte(i%251);
        auto path=package/"cas_01.cas";
        {std::ofstream file(path,std::ios::binary);file.write(reinterpret_cast<const char*>(bytes.data()),bytes.size());check(bool(file),"Fixture write failed");}
        db::Node name;name.name="name";name.named=true;name.type=7;name.text="test";
        db::Node id;id.name="persistentIndex";id.named=true;id.type=8;id.owned={7,0,0,0};
        db::Node entry;entry.type=19;entry.children={name,id};
        db::Node chunks;chunks.name="installChunks";chunks.named=true;chunks.type=19;chunks.children={entry};
        db::Node manifest;manifest.name="installManifest";manifest.named=true;manifest.type=19;manifest.children={chunks};
        db::Node layout;layout.type=19;layout.children={manifest};
        dingosdk::vfs::GameArchives archives(root,layout);archives.keep_open();
        fb::CasIdentifier location{false,7,1};
        check(archives.archive_size(root,location)==bytes.size(),"Archive availability cache failed");
        check(!archives.archive_size(root,{false,7,2}),"Missing archive availability was incorrect");
        std::atomic<unsigned> next{0};std::atomic<bool> failed{false};
        std::vector<std::jthread> threads;
        for(int worker=0;worker<8;++worker)threads.emplace_back([&] {
            try {
                for(unsigned i=next.fetch_add(1);i<2048;i=next.fetch_add(1)) {
                    auto offset=static_cast<unsigned>((i*997u)%(bytes.size()-4096));
                    auto result=archives.read(root,location,offset,4096);
                    check(result.size()==4096,"Concurrent read length differs");
                    for(size_t j=0;j<result.size();++j)check(result[j]==bytes[offset+j],"Concurrent read used another request's offset");
                }
            } catch(...) {failed=true;}
        });
        // Dropping the handle cache must not close a handle in use by a worker.
        for(int i=0;i<32;++i){archives.forget(path);std::this_thread::yield();}
        for(auto& thread:threads)thread.join();
        check(!failed,"Parallel archive reads failed");
        check(archives.read(root,location,static_cast<unsigned>(bytes.size()),0).empty(),"Empty read failed");
        rejects([&]{(void)archives.read(root,location,static_cast<unsigned>(bytes.size()-1),2);});
        rejects([&]{(void)archives.read(root,{false,7,2},0,1);});
        std::vector<std::byte> encoded,expected;
        for(unsigned i=0;i<100;++i) {
            // Big-endian 24-bit decoded size, raw codec, big-endian encoded size.
            for(unsigned b:{0u,0u,16u,0u,0u,0u,16u,0u})encoded.push_back(std::byte(b));
            encoded.insert(encoded.end(),bytes.begin()+i*4096,bytes.begin()+(i+1)*4096);
            expected.insert(expected.end(),bytes.begin()+i*4096,bytes.begin()+(i+1)*4096);
        }
        check(fb::decode_cas(encoded)==expected,"Multi-block raw CAS output differs");
        rejects([&]{(void)fb::decode_cas(encoded,{{},expected.size()-1});});
        rejects([&]{(void)fb::decode_cas(std::span<const std::byte>(encoded).first(5));});
        encoded.pop_back();rejects([&]{(void)fb::decode_cas(encoded);});
        check(fb::decode_cas({}).empty(),"Empty CAS output differs");
        archives.forget(path);
        fs::remove_all(root);
        std::cout<<"Concurrent CAS offsets, handle lifetime, bounds and raw block decoding passed\n";
        return 0;
    } catch(const std::exception& e) {
        std::error_code ignored;fs::remove_all(root,ignored);
        std::cerr<<"Archive self-test failed: "<<e.what()<<"\n";return 1;
    }
}
