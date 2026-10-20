cmake_minimum_required(VERSION 3.20)
project(doubao-voice-input VERSION 0.1.0 LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

find_package(Fcitx5Core 5.1.23 REQUIRED)
find_package(Fcitx5ModuleDBus REQUIRED)
find_package(Fcitx5Utils REQUIRED)

add_library(doubaovoiceinput MODULE src/fcitx5/doubaovoiceinput.cpp)
target_compile_definitions(
    doubaovoiceinput
    PRIVATE
        FCITX_ENABLE_DEBUG
)
target_link_libraries(
    doubaovoiceinput
    PRIVATE
        Fcitx5::Core
        Fcitx5::Utils
        Fcitx5::Module::DBus
)
target_compile_options(
    doubaovoiceinput
    PRIVATE
        -Wall
        -Wextra
        -Wpedantic
)
set_target_properties(doubaovoiceinput PROPERTIES PREFIX "")

install(
    TARGETS doubaovoiceinput
    LIBRARY DESTINATION "${FCITX_INSTALL_ADDONDIR}"
)
install(
    FILES data/doubaovoiceinput.conf
    DESTINATION "${FCITX_INSTALL_PKGDATADIR}/addon"
)
