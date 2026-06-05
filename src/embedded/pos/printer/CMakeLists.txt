# Case 7 ESC/POS thermal printer protocol CMake fragment (OP-2052).
#
# Include this fragment from the embedded platform CMake entry point. Cross
# compilation is supplied by the caller through the platform toolchain file.

add_library(omnisight-pos-printer-escpos STATIC
  ${CMAKE_CURRENT_LIST_DIR}/escpos-impl.cpp
)

target_include_directories(omnisight-pos-printer-escpos PUBLIC
  ${CMAKE_CURRENT_LIST_DIR}
)

target_compile_features(omnisight-pos-printer-escpos PUBLIC
  cxx_std_17
)
target_compile_options(omnisight-pos-printer-escpos PRIVATE
  -Wall
  -Wextra
  -Werror
)

add_executable(omnisight-pos-printer-escpos-smoke
  ${CMAKE_CURRENT_LIST_DIR}/escpos-impl.cpp
)
target_compile_definitions(omnisight-pos-printer-escpos-smoke PRIVATE
  OMNISIGHT_POS_PRINTER_ESCPOS_IMPL_SMOKE_MAIN=1
)
target_include_directories(omnisight-pos-printer-escpos-smoke PRIVATE
  ${CMAKE_CURRENT_LIST_DIR}
)
target_compile_features(omnisight-pos-printer-escpos-smoke PRIVATE
  cxx_std_17
)
target_compile_options(omnisight-pos-printer-escpos-smoke PRIVATE
  -Wall
  -Wextra
  -Werror
)
