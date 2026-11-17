cmake_minimum_required(VERSION 3.16)
project(lib_sdls VERSION 0.3.0 LANGUAGES C)

# Ground side SDLP/SDLS library, loaded by the Python ground system through ctypes
# (sdlp_link.py, used by MiniCmdUtil.py and RoutingService.py).

# --- build mode -----------------------------------------------------------------------------
# ON  (default): SDLP only. Space Packets are framed into TC frames and taken out of TM
#                frames; the Security Header (14 B) and Trailer (16 B) are dummy zeros.
#                Builds src/sdlp only: no SA DB, no crypto, no wolfSSL needed.
# OFF          : full SDLS (SA DB, AES-256-GCM, PQC key exchange) on wolfSSL.
# The SDLS sources stay in the tree in both modes; ON only leaves them out of the build.
option(SDLS_SDLP_ONLY "Build SDLP framing only (no Security Header and no MAC on TC and TM)" ON)

set(SDLS_SDLP_SOURCES
  src/sdlp/tc_sdlp_my.c
  src/sdlp/tm_sdlp_my.c)

if(NOT SDLS_SDLP_ONLY)
  # --- crypto backend -----------------------------------------------------------------------
  # One backend file implements src/crypto/crypto_my.h. A partner implementation adds its own
  # file and a value here; nothing outside src/crypto changes.
  set(SDLS_CRYPTO_BACKEND "wolfssl" CACHE STRING "Crypto backend (wolfssl)")
  set_property(CACHE SDLS_CRYPTO_BACKEND PROPERTY STRINGS wolfssl)

  if(SDLS_CRYPTO_BACKEND STREQUAL "wolfssl")
    # wolfSSL built with: --enable-aesgcm --enable-mlkem --enable-mldsa (HKDF is on by default).
    # Point WOLFSSL_ROOT (or CMAKE_PREFIX_PATH / PKG_CONFIG_PATH) at a non system install.
    set(WOLFSSL_ROOT "" CACHE PATH "wolfSSL install prefix (optional)")
    if(WOLFSSL_ROOT)
      list(APPEND CMAKE_PREFIX_PATH ${WOLFSSL_ROOT})
    endif()
    find_package(PkgConfig QUIET)
    if(PkgConfig_FOUND)
      pkg_check_modules(WOLFSSL QUIET IMPORTED_TARGET wolfssl)
    endif()
    if(TARGET PkgConfig::WOLFSSL)
      set(SDLS_CRYPTO_LIBS PkgConfig::WOLFSSL)
    else()
      find_path(WOLFSSL_INCLUDE_DIR wolfssl/options.h REQUIRED)
      find_library(WOLFSSL_LIBRARY wolfssl REQUIRED)
      add_library(sdls_wolfssl INTERFACE)
      target_include_directories(sdls_wolfssl INTERFACE ${WOLFSSL_INCLUDE_DIR})
      target_link_libraries(sdls_wolfssl INTERFACE ${WOLFSSL_LIBRARY})
      set(SDLS_CRYPTO_LIBS sdls_wolfssl)
    endif()
    set(SDLS_CRYPTO_SOURCES src/crypto/crypto_wolfssl.c src/crypto/crypto_registry.c)
  else()
    message(FATAL_ERROR "Unknown SDLS_CRYPTO_BACKEND '${SDLS_CRYPTO_BACKEND}'")
  endif()

  set(SDLS_SECURITY_SOURCES
    src/sa/sa_default.c
    src/sa/sa_shm_my.c
    src/sdls/sechdr_my.c
    src/sdls/sdls_my.c
    src/ep/ep_my.c
    ${SDLS_CRYPTO_SOURCES})
endif()

# --- libsdls.so -----------------------------------------------------------------------------
add_library(sdls SHARED ${SDLS_SDLP_SOURCES} ${SDLS_SECURITY_SOURCES})

target_include_directories(sdls PUBLIC ${CMAKE_CURRENT_SOURCE_DIR}/include)

if(SDLS_SDLP_ONLY)
  target_compile_definitions(sdls PRIVATE SDLS_MY_SDLP_ONLY)
  message(STATUS "lib_sdls: SDLP-only build (Security Header/Trailer zero filled, no crypto)")
else()
  # SA/Key DB in POSIX shared memory: process-shared mutex (pthread), shm_open (librt on
  # glibc older than 2.34; newer glibc has it in libc and librt is an empty stub).
  find_package(Threads REQUIRED)
  find_library(RT_LIBRARY rt)
  target_link_libraries(sdls PRIVATE Threads::Threads ${SDLS_CRYPTO_LIBS})
  if(RT_LIBRARY)
    target_link_libraries(sdls PRIVATE ${RT_LIBRARY})
  endif()
  message(STATUS "lib_sdls: SDLS build (SA DB, AES-256-GCM, key exchange on ${SDLS_CRYPTO_BACKEND})")
endif()

set_target_properties(sdls PROPERTIES
  C_STANDARD 99
  C_STANDARD_REQUIRED ON
  POSITION_INDEPENDENT_CODE ON
  VERSION   ${PROJECT_VERSION}
  SOVERSION ${PROJECT_VERSION_MAJOR})

target_compile_options(sdls PRIVATE -Wall -Wextra)

# --- tests ----------------------------------------------------------------------------------
# Python ctypes tests in test/ (run with: ctest --test-dir build --output-on-failure).
# Tests that need SDLS skip themselves in an SDLP-only build.
# SDLS build only: libsdls_satsim.so, a test only stand-in for the satellite crypto (ML-KEM
# key pair and decaps, ML-DSA verify, AES-GCM), built from the same backend.
option(SDLS_BUILD_TESTS "Register the Python tests (and build the test satellite library)" ON)
if(SDLS_BUILD_TESTS)
  set(SDLS_TEST_ENV "SDLS_LIB=$<TARGET_FILE:sdls>")
  if(NOT SDLS_SDLP_ONLY)
    add_library(sdls_satsim SHARED test/satsim/satsim.c ${SDLS_CRYPTO_SOURCES})
    target_include_directories(sdls_satsim PRIVATE ${CMAKE_CURRENT_SOURCE_DIR}/include
                                                   ${CMAKE_CURRENT_SOURCE_DIR}/src)
    target_link_libraries(sdls_satsim PRIVATE Threads::Threads ${SDLS_CRYPTO_LIBS})
    set_target_properties(sdls_satsim PROPERTIES C_STANDARD 99 POSITION_INDEPENDENT_CODE ON)
    target_compile_options(sdls_satsim PRIVATE -Wall -Wextra)
    list(APPEND SDLS_TEST_ENV "SDLS_SATSIM_LIB=$<TARGET_FILE:sdls_satsim>")
  endif()

  enable_testing()
  find_package(Python3 COMPONENTS Interpreter)
  if(Python3_Interpreter_FOUND)
    add_test(NAME python_sdlp
      COMMAND ${Python3_EXECUTABLE} -m unittest discover -s ${CMAKE_CURRENT_SOURCE_DIR}/test -v)
    set_tests_properties(python_sdlp PROPERTIES ENVIRONMENT "${SDLS_TEST_ENV}")
  endif()
endif()
