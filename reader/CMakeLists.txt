cmake_minimum_required(VERSION 3.21)
project(AuraFrostbiteReader LANGUAGES CXX)
set(CMAKE_CXX_STANDARD 20)
set(SDK vendor/reskate)
add_executable(aura_frostbite_reader main.cpp cas_decode.cpp archive_tests.cpp
 ${SDK}/Engine/Resource/binary_io.cpp ${SDK}/Engine/Resource/binary_bundle.cpp
 ${SDK}/Engine/Resource/toc.cpp ${SDK}/Engine/Resource/ebx_document.cpp
 ${SDK}/Engine/Resource/texture.cpp ${SDK}/Engine/Resource/mesh_set.cpp
 ${SDK}/Engine/Vfs/native_db.cpp ${SDK}/Engine/Vfs/game_archives.cpp)
target_include_directories(aura_frostbite_reader PRIVATE vendor ${SDK} ${SDK}/External/bcdec)
target_compile_definitions(aura_frostbite_reader PRIVATE WIN32_LEAN_AND_MEAN NOMINMAX)
if(MSVC)
    target_compile_options(aura_frostbite_reader PRIVATE /FS)
endif()
