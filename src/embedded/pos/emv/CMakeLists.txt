# Case 7 EMV ISO 7816 contact smart-card reader CMake fragment (OP-2024).
#
# Include this fragment from the embedded platform CMake entry point. Cross
# compilation is supplied by the caller through the platform toolchain file.

add_library(omnisight-pos-emv-iso7816-contact-reader STATIC
  ${CMAKE_CURRENT_LIST_DIR}/iso7816-contact-reader.cpp
)

target_include_directories(omnisight-pos-emv-iso7816-contact-reader PUBLIC
  ${CMAKE_CURRENT_LIST_DIR}
)

target_compile_features(omnisight-pos-emv-iso7816-contact-reader PUBLIC
  cxx_std_17
)
target_compile_options(omnisight-pos-emv-iso7816-contact-reader PRIVATE
  -Wall
  -Wextra
  -Werror
)
