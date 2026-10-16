#include "vendor/json.hpp"
#include "vendor/reskate/Engine/Resource/toc.h"
#include "vendor/reskate/Engine/Resource/ebx_document.h"
#include "vendor/reskate/Engine/Resource/cas_codec.h"
#include "vendor/reskate/Engine/Resource/texture.h"
#include "vendor/reskate/Engine/Vfs/game_archives.h"
#include <filesystem>
#include <fstream>
#include <iostream>
#include <map>
#include <set>
#include <thread>
#include <atomic>
#include <cstdlib>
#include <algorithm>
#include <mutex>
#include <chrono>
using Json = nlohmann::json;
namespace fs=std::filesystem;
namespace fb=dingosdk::frostbite;
namespace ebx=fb::ebx;
std::vector<std::byte> read(const fs::path& path) {
 std::ifstream file(path,std::ios::binary|std::ios::ate);
 if(!file) throw std::runtime_error("Cannot open "+path.string());
 auto size=file.tellg(); if(size<0 || size>512LL*1024*1024) throw std::runtime_error("Invalid input size");
 std::vector<std::byte> result(static_cast<size_t>(size)); file.seekg(0);
 if(!file.read(reinterpret_cast<char*>(result.data()),result.size())) throw std::runtime_error("Short read");
 return result;
}
void write(const fs::path& path,std::span<const std::byte> data) {
 fs::create_directories(path.parent_path());std::ofstream f(path,std::ios::binary);
 if(!f.write(reinterpret_cast<const char*>(data.data()),data.size())) throw std::runtime_error("Output write failed");
}
void writeJson(const fs::path& path,const Json& data) {
 fs::create_directories(path.parent_path()); std::ofstream f(path); f<<data.dump(2);
 if(!f) throw std::runtime_error("JSON write failed");
}
template<size_t N> std::string hex(const std::array<std::byte,N>& data) {
 static const char* digits="0123456789abcdef";std::string result;
 for(auto b:data) {auto v=std::to_integer<unsigned>(b);result+=digits[v>>4];result+=digits[v&15];}return result;
}
Json value(const ebx::Value& v,const ebx::Document& doc,unsigned depth=0);
Json object(const std::shared_ptr<ebx::Object>& obj,const ebx::Document& doc,unsigned depth) {
 if(!obj) return nullptr;
 if(depth>64) throw std::runtime_error("EBX object nesting exceeded");
 Json result=Json::object();
 result["$type"]=obj->descriptor>=0?doc.types.at(obj->descriptor).name:"";
 for(const auto& field:obj->fields) result[field.name]=value(field.value,doc,depth+1);
 return result;
}
Json value(const ebx::Value& v,const ebx::Document& doc,unsigned depth) {
 return std::visit([&](const auto& x)->Json {
  using T=std::decay_t<decltype(x)>;
  if constexpr(std::is_same_v<T,std::monostate>) return nullptr;
  else if constexpr(std::is_same_v<T,fb::Guid>) return hex(x.bytes);
  else if constexpr(std::is_same_v<T,fb::Sha1>) return hex(x.bytes);
  else if constexpr(std::is_same_v<T,ebx::ResourceReference>) return Json{{"$resource",std::to_string(x.id)}};
  else if constexpr(std::is_same_v<T,ebx::PointerReference>) return Json{{"$pointer",int(x.kind)},{"index",x.index}};
  else if constexpr(std::is_same_v<T,ebx::TypeReference>) return Json{{"$descriptor",x.descriptor},{"encoded",x.encoded}};
  else if constexpr(std::is_same_v<T,ebx::BoxedReference>) return Json{{"$boxed",x.dataOffset},{"type",x.encodedType}};
  else if constexpr(std::is_same_v<T,std::shared_ptr<ebx::Object>>) return object(x,doc,depth);
  else if constexpr(std::is_same_v<T,ebx::Value::Array>) {Json result=Json::array();for(const auto& e:x) result.push_back(value(e,doc,depth+1));return result;}
  else return x;
 },v.data);
}
Json document(const ebx::Document& doc) {
 Json result={{"guid",hex(doc.fileGuid.bytes)},{"root_type",doc.rootType},{"objects",Json::array()},{"imports",Json::array()}};
 for(const auto& instance:doc.instances) {
  auto obj=object(instance.object,doc,0);obj["$guid"]=hex(instance.instanceGuid.bytes);result["objects"].push_back(obj);
 }
 for(const auto& ref:doc.imports) result["imports"].push_back({{"file",hex(ref.fileGuid.bytes)},{"object",hex(ref.classGuid.bytes)}});
 return result;
}
int archiveSelfTest();
int main(int argc,char** argv) {
 if(argc==2 && std::string(argv[1])=="--test-archives") return archiveSelfTest();
 if(argc==4 && std::string(argv[1])=="--textures") {
  try {
   fs::path folder=argv[2],out=argv[3];std::ifstream f(folder/"inventory.json");Json inventory;f>>inventory;
   std::map<std::string,fs::path> chunks;
   for(const auto& a:inventory["assets"]) if(a["kind"]=="chunk" && a.contains("bytes")) chunks[a["guid"]]=folder/a["path"].get<std::string>();
   Json decoded=Json::object(),errors=Json::array();
   std::vector<Json> work;
   std::set<std::string> unique;
   for(const auto& a:inventory["assets"]) if(a.value("resource_type",0u)==fb::texture_resource_type && a.contains("bytes") && unique.insert(a["resource_id"].get<std::string>()).second) work.push_back(a);
   std::vector<Json> results(work.size()),failures(work.size());
   unsigned workers=std::clamp(std::thread::hardware_concurrency(),1u,4u);
   if(const char* setting=std::getenv("AURA_IMPORT_JOBS")) workers=std::clamp(std::atoi(setting),1,8);
   unsigned textureLimit=2048;
   bool capTextureSize=false;
   if(const char* setting=std::getenv("AURA_IMPORT_TEXTURE_SIZE")) {textureLimit=std::clamp(std::atoi(setting),128,2048);capTextureSize=true;}
   std::atomic<size_t> next{0};
   auto decode=[&] {
    for(size_t i=next.fetch_add(1);i<work.size();i=next.fetch_add(1)) {
     const auto& a=work[i];
    try {
     std::vector<std::byte> meta;for(const auto& b:a["metadata"])meta.push_back(std::byte(b.get<unsigned>()));
     auto header=fb::read_texture_header(read(folder/a["path"].get<std::string>()),meta);
     auto chunk=chunks.find(hex(header.chunk.bytes));if(chunk==chunks.end())throw std::runtime_error("Texture chunk unavailable");
     auto image=fb::decode_texture(header,read(chunk->second),textureLimit);
     const auto dimension=std::max(image.width,image.height);
     if(capTextureSize && dimension>textureLimit) image=fb::resize(image,std::max(1u,image.width*textureLimit/dimension),std::max(1u,image.height*textureLimit/dimension));
     auto name=a["resource_id"].get<std::string>()+".rgba";
     std::vector<std::byte> raw(8+image.rgba.size());std::memcpy(raw.data(),&image.width,4);std::memcpy(raw.data()+4,&image.height,4);std::memcpy(raw.data()+8,image.rgba.data(),image.rgba.size());
     write(out/name,raw);results[i]={{"path",name},{"width",image.width},{"height",image.height},{"format",header.format}};
    } catch(const std::exception& e) { failures[i]={{"name",a["name"]},{"resource_id",a["resource_id"]},{"error",e.what()}}; }
    }
   };
   std::vector<std::jthread> threads;
   for(unsigned i=0;i<std::min<size_t>(workers,work.size());++i) threads.emplace_back(decode);
   for(auto& thread:threads) thread.join();
   for(size_t i=0;i<work.size();++i) {
    if(!results[i].is_null()) decoded[work[i]["resource_id"].get<std::string>()]=std::move(results[i]);
    if(!failures[i].is_null()) errors.push_back(std::move(failures[i]));
   }
   writeJson(out/"textures.json",{{"textures",decoded},{"errors",errors}});std::cout<<"Decoded "<<decoded.size()<<" textures; "<<errors.size()<<" errors\n";return errors.empty()?0:3;
  } catch(const std::exception& e) {std::cerr<<e.what();return 2;}
 }
 if(argc!=4) {std::cerr<<"usage: aura_frostbite_reader game-root mod-root output-directory\n";return 1;}
 try {
  fs::path game=argv[1],mod=argv[2],out=argv[3];fs::create_directories(out);
  auto layout=dingosdk::vfs::read_layout(mod/"layout.toc");
  dingosdk::vfs::GameArchives archives(mod,layout.root);archives.keep_open();
  Json inventory={{"version",1},{"assets",Json::array()},{"errors",Json::array()}};
  std::set<std::string> seen;
  size_t externalReferences=0;
  struct ExportWork {
   fb::BundleAsset asset;fb::BundleFileInfo file;fs::path path;
   Json item;std::vector<Json> errors;std::shared_ptr<std::mutex> pathLock;
  };
  struct Event {bool bundleError;size_t index;};
  std::vector<ExportWork> work;
  std::vector<Json> bundleErrors;
  std::vector<Event> events;
  std::map<fs::path,std::shared_ptr<std::mutex>> pathLocks;
  const auto scanStarted=std::chrono::steady_clock::now();
  auto exportAsset=[&](const fb::BundleAsset& a,const fb::BundleFileInfo& f,const std::string& bundle) {
   auto kind=a.kind==fb::AssetKind::ebx?"ebx":a.kind==fb::AssetKind::resource?"resource":"chunk";
   if(!archives.archive_size(mod,f.location)) {
    ++externalReferences;return;
   }
   std::string key=std::string(kind)+":"+(a.kind==fb::AssetKind::chunk?hex(a.guid.bytes):a.name);
   if(!seen.insert(key).second) return;
   std::string stem=hex(a.sha1.bytes); if(stem==std::string(40,'0')) stem=hex(a.guid.bytes);
   fs::path relative=fs::path(kind)/(stem+".bin");
   Json item={{"kind",kind},{"name",a.name},{"guid",hex(a.guid.bytes)},{"resource_id",std::to_string(a.resourceId)},
    {"resource_type",a.resourceType},{"metadata",hex(std::array<std::byte,0>{})},{"path",relative.generic_string()},{"bundle",bundle}};
   item["metadata"]=Json::array();for(auto b:a.resourceMeta)item["metadata"].push_back(std::to_integer<unsigned>(b));
   auto [lock,added]=pathLocks.try_emplace(relative);
   if(added) lock->second=std::make_shared<std::mutex>();
   events.push_back({false,work.size()});
   work.push_back({a,f,relative,std::move(item),{},lock->second});
  };
  for(const auto& entry:fs::recursive_directory_iterator(mod/"Win32")) if(entry.path().extension()==".toc") {
   auto toc=fb::read_toc(read(entry.path()));std::cout<<entry.path().filename().string()<<": "<<toc.bundles.size()<<" bundles\n";
   for(const auto& b:toc.bundles) {
    try {
     auto region=fb::read_bundle_region(b.region);fb::BinaryBundle manifest;
     size_t payload=0;
     if(!region.inlineManifest.empty()) manifest=fb::read_binary_bundle(region.inlineManifest);
     else {if(region.files.empty())continue;const auto& f=region.files[0];manifest=archives.read_manifest(mod,f.location,f.offset,f.size,game);payload=1;}
     for(const auto* list:{&manifest.ebx,&manifest.resources,&manifest.chunks})for(const auto& a:*list) {
      if(payload>=region.files.size())throw std::runtime_error("Bundle has fewer payloads than assets");exportAsset(a,region.files[payload++],b.name);
     }
    } catch(const std::exception& e) {
     events.push_back({true,bundleErrors.size()});bundleErrors.push_back({{"bundle",b.name},{"error",e.what()}});
    }
   }
   for(const auto& chunk:toc.chunks) if(!chunk.removed) {
    fb::BundleAsset a;a.kind=fb::AssetKind::chunk;a.guid=chunk.guid;
    exportAsset(a,{chunk.location,chunk.offset,chunk.size},entry.path().filename().string());
   }
  }
  const auto scanFinished=std::chrono::steady_clock::now();
  unsigned workers=std::clamp(std::thread::hardware_concurrency(),1u,4u);
  if(const char* setting=std::getenv("AURA_IMPORT_JOBS")) workers=std::clamp(std::atoi(setting),1,8);
  std::atomic<size_t> next{0};
  auto extract=[&] {
   for(size_t i=next.fetch_add(1);i<work.size();i=next.fetch_add(1)) {
    auto& job=work[i];const auto& a=job.asset;const auto& f=job.file;auto& item=job.item;
    try {
     // Aliases can share a SHA-derived output filename. Never write it concurrently.
     std::lock_guard lock(*job.pathLock);
     if(f.size>512u*1024u*1024u) throw std::runtime_error("Encoded asset exceeds input size limit");
     auto raw=archives.read(mod,f.location,f.offset,f.size);auto bytes=fb::decode_cas(raw,{game});
     if(bytes.size()!=a.originalSize && a.kind!=fb::AssetKind::chunk) throw std::runtime_error("Decoded asset size differs from manifest");
     write(out/job.path,bytes);item["bytes"]=bytes.size();
     if(a.kind==fb::AssetKind::ebx) {
      try {
       auto doc=ebx::read_document(bytes);auto jsonPath=job.path;jsonPath.replace_extension(".json");
       writeJson(out/jsonPath,document(doc));item["document"]=jsonPath.generic_string();item["file_guid"]=hex(doc.fileGuid.bytes);item["root_type"]=doc.rootType;
      } catch(const std::exception& e) {
       item["decode_error"]=e.what();job.errors.push_back({{"name",a.name},{"stage","ebx"},{"error",e.what()}});
      }
     }
    } catch(const std::exception& e) {
     item["error"]=e.what();job.errors.push_back({{"name",a.name},{"stage","asset"},{"error",e.what()}});
    }
   }
  };
  std::vector<std::jthread> threads;
  for(unsigned i=0;i<std::min<size_t>(workers,work.size());++i) threads.emplace_back(extract);
  for(auto& thread:threads) thread.join();
  // Merge in discovery order, never worker-completion order.
  for(const auto& event:events) {
   if(event.bundleError) inventory["errors"].push_back(std::move(bundleErrors[event.index]));
   else {
    auto& job=work[event.index];inventory["assets"].push_back(std::move(job.item));
    for(auto& error:job.errors) inventory["errors"].push_back(std::move(error));
   }
  }
  inventory["external_archive_references"]=externalReferences;
  writeJson(out/"inventory.json",inventory);
  std::cout<<"Exported "<<inventory["assets"].size()<<" assets; "<<inventory["errors"].size()<<" errors\n";
  const auto finished=std::chrono::steady_clock::now();
  std::cout<<"Archive scan "<<std::chrono::duration<double>(scanFinished-scanStarted).count()<<"s; extraction "
           <<std::chrono::duration<double>(finished-scanFinished).count()<<"s; "<<workers<<" worker limit\n";
  return inventory["assets"].empty()?2:0;
 } catch(const std::exception& e) {std::cerr<<e.what()<<"\n";return 2;}
}
