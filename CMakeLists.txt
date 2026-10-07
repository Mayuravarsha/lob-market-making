# CMakeLists.txt
#
# CMake is a build-system generator: it reads this file and produces the actual
# build files for your platform (Makefiles on Linux, a Visual Studio solution on
# Windows). You never edit those; you edit this.
#
#   cmake -S . -B build -DCMAKE_BUILD_TYPE=Release
#   cmake --build build -j
#
# FetchContent downloads pinned versions of pybind11 and Google Benchmark at
# configure time, so there is nothing to install by hand. Pinning to an exact
# tag (not "main") is what makes the build reproducible a year from now.
cmake_minimum_required(VERSION 3.16)
project(lob_backtester VERSION 0.1.0 LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)
set(CMAKE_POSITION_INDEPENDENT_CODE ON)   # required for the Python .so

if(NOT CMAKE_BUILD_TYPE AND NOT CMAKE_CONFIGURATION_TYPES)
  set(CMAKE_BUILD_TYPE Release CACHE STRING "" FORCE)
endif()

option(LOB_BUILD_TESTS      "Build the correctness tests"  ON)
option(LOB_BUILD_BENCH      "Build the Google Benchmark suite" ON)
option(LOB_BUILD_PYTHON     "Build the pybind11 module"    ON)
option(LOB_NATIVE_ARCH      "Compile with -march=native"   OFF)

# ---------------------------------------------------------------- warnings
if(MSVC)
  add_compile_options(/W4 /permissive-)
else()
  add_compile_options(-Wall -Wextra -Wpedantic)
endif()

# -march=native lets the compiler use every instruction your own CPU supports.
# Great for benchmarking on your machine, but the binary may crash on an older
# CPU -- so it is opt-in, not default.
if(LOB_NATIVE_ARCH AND NOT MSVC)
  add_compile_options(-march=native)
endif()

# ------------------------------------------------------------- core library
add_library(lob_core STATIC src/order_book.cpp)
target_include_directories(lob_core PUBLIC
  $<BUILD_INTERFACE:${CMAKE_CURRENT_SOURCE_DIR}/include>)

include(FetchContent)

# ------------------------------------------------------------------- tests
if(LOB_BUILD_TESTS)
  add_executable(test_order_book tests/test_order_book.cpp)
  target_link_libraries(test_order_book PRIVATE lob_core)
  enable_testing()
  add_test(NAME order_book COMMAND test_order_book)
endif()

# --------------------------------------------------------------- benchmarks
if(LOB_BUILD_BENCH)
  set(BENCHMARK_ENABLE_TESTING     OFF CACHE BOOL "" FORCE)
  set(BENCHMARK_ENABLE_GTEST_TESTS OFF CACHE BOOL "" FORCE)
  set(BENCHMARK_ENABLE_INSTALL     OFF CACHE BOOL "" FORCE)
  set(BENCHMARK_INSTALL_DOCS       OFF CACHE BOOL "" FORCE)
  # Google Benchmark builds ITSELF with -Werror by default (BENCHMARK_ENABLE_WERROR).
  # On GCC 16.2 that trips a real warning in benchmark's own sysinfo.cc (an
  # unhandled enum case in a switch) that didn't exist against older/other
  # compilers. This only relaxes warnings-as-errors for the vendored
  # dependency's own build -- our own targets below keep -Wall -Wextra
  # -Wpedantic and are unaffected.
  set(BENCHMARK_ENABLE_WERROR      OFF CACHE BOOL "" FORCE)
  FetchContent_Declare(benchmark
    GIT_REPOSITORY https://github.com/google/benchmark.git
    GIT_TAG        v1.9.1
    GIT_SHALLOW    TRUE)
  FetchContent_MakeAvailable(benchmark)

  add_executable(bench_order_book bench/bench_order_book.cpp)
  target_link_libraries(bench_order_book PRIVATE lob_core benchmark::benchmark)
endif()

# Percentile latency profiler -- no external dependency, always built.
add_executable(latency_profile bench/latency_profile.cpp)
target_link_libraries(latency_profile PRIVATE lob_core)

# ----------------------------------------------------------- python module
if(LOB_BUILD_PYTHON)
  FetchContent_Declare(pybind11
    GIT_REPOSITORY https://github.com/pybind/pybind11.git
    GIT_TAG        v2.13.6
    GIT_SHALLOW    TRUE)
  FetchContent_MakeAvailable(pybind11)

  pybind11_add_module(_lob bindings/py_lob.cpp)
  target_link_libraries(_lob PRIVATE lob_core)

  # MinGW builds: since Python 3.8, extension-module imports on Windows no
  # longer search PATH for dependent DLLs (only the module's own directory
  # and paths added via os.add_dll_directory()). A dynamically-linked MinGW
  # build of _lob would need libgcc/libstdc++/libwinpthread's DLLs shipped
  # alongside it or that call made first -- easy to get wrong for anyone who
  # just does `import lobkit`. Statically linking the MinGW runtime into the
  # module removes the dependency entirely, so plain `import lobkit` works
  # with nothing extra on the Python side.
  if(MINGW)
    target_link_options(_lob PRIVATE -static-libgcc -static-libstdc++ -static)
  endif()

  # Drop the built module straight into the Python package so that
  # `import lobkit` finds it with no install step.
  set_target_properties(_lob PROPERTIES
    LIBRARY_OUTPUT_DIRECTORY "${CMAKE_CURRENT_SOURCE_DIR}/python/lobkit")
endif()

message(STATUS "")
message(STATUS "lob_backtester configured")
message(STATUS "  build type : ${CMAKE_BUILD_TYPE}")
message(STATUS "  tests      : ${LOB_BUILD_TESTS}")
message(STATUS "  benchmarks : ${LOB_BUILD_BENCH}")
message(STATUS "  python     : ${LOB_BUILD_PYTHON}")
message(STATUS "")
