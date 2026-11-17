# CMake generated Testfile for 
# Source directory: /home/yujin/Documents/lab-2026-1/lib-sdls
# Build directory: /home/yujin/Documents/lab-2026-1/lib-sdls/build
# 
# This file includes the relevant testing commands required for 
# testing this directory and lists subdirectories to be tested as well.
add_test(python_sdlp "/usr/bin/python3" "-m" "unittest" "discover" "-s" "/home/yujin/Documents/lab-2026-1/lib-sdls/test" "-v")
set_tests_properties(python_sdlp PROPERTIES  ENVIRONMENT "SDLS_LIB=/home/yujin/Documents/lab-2026-1/lib-sdls/build/libsdls.so.0.2.0;SDLS_SATSIM_LIB=/home/yujin/Documents/lab-2026-1/lib-sdls/build/libsdls_satsim.so" _BACKTRACE_TRIPLES "/home/yujin/Documents/lab-2026-1/lib-sdls/CMakeLists.txt;85;add_test;/home/yujin/Documents/lab-2026-1/lib-sdls/CMakeLists.txt;0;")
