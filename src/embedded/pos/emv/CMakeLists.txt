# Case 7 EMV contact/contactless smart-card reader + PED + cryptogram CMake fragment
# (OP-2024/OP-2032/OP-2041/OP-2047).
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

add_library(omnisight-pos-emv-iso14443-contactless-reader STATIC
  ${CMAKE_CURRENT_LIST_DIR}/iso14443-contactless-reader.cpp
)

target_include_directories(omnisight-pos-emv-iso14443-contactless-reader PUBLIC
  ${CMAKE_CURRENT_LIST_DIR}
)

target_compile_features(omnisight-pos-emv-iso14443-contactless-reader PUBLIC
  cxx_std_17
)
target_compile_options(omnisight-pos-emv-iso14443-contactless-reader PRIVATE
  -Wall
  -Wextra
  -Werror
)
target_link_libraries(omnisight-pos-emv-iso14443-contactless-reader PUBLIC
  omnisight-pos-emv-iso7816-contact-reader
)

add_library(omnisight-pos-emv-ped-pin-entry STATIC
  ${CMAKE_CURRENT_LIST_DIR}/ped-pin-entry.cpp
)

target_include_directories(omnisight-pos-emv-ped-pin-entry PUBLIC
  ${CMAKE_CURRENT_LIST_DIR}
)

target_compile_features(omnisight-pos-emv-ped-pin-entry PUBLIC
  cxx_std_17
)
target_compile_options(omnisight-pos-emv-ped-pin-entry PRIVATE
  -Wall
  -Wextra
  -Werror
)

add_executable(omnisight-pos-emv-ped-pin-entry-smoke
  ${CMAKE_CURRENT_LIST_DIR}/ped-pin-entry.cpp
)
target_compile_definitions(omnisight-pos-emv-ped-pin-entry-smoke PRIVATE
  OMNISIGHT_PED_PIN_ENTRY_SMOKE_MAIN=1
)
target_compile_features(omnisight-pos-emv-ped-pin-entry-smoke PRIVATE
  cxx_std_17
)
target_compile_options(omnisight-pos-emv-ped-pin-entry-smoke PRIVATE
  -Wall
  -Wextra
  -Werror
)

# ── EMV cryptogram generator (OP-2047) ──────────────────────────────

add_library(omnisight-pos-emv-cryptogram-generator STATIC
  ${CMAKE_CURRENT_LIST_DIR}/cryptogram-generator.cpp
)
target_include_directories(omnisight-pos-emv-cryptogram-generator PUBLIC
  ${CMAKE_CURRENT_LIST_DIR}
)
target_compile_features(omnisight-pos-emv-cryptogram-generator PUBLIC cxx_std_17)
target_compile_options(omnisight-pos-emv-cryptogram-generator PRIVATE -Wall -Wextra -Werror)

add_executable(omnisight-pos-emv-cryptogram-generator-smoke
  ${CMAKE_CURRENT_LIST_DIR}/cryptogram-generator.cpp
)
target_compile_definitions(omnisight-pos-emv-cryptogram-generator-smoke PRIVATE
  OMNISIGHT_EMV_CRYPTOGRAM_GENERATOR_SMOKE_MAIN=1
)
target_compile_features(omnisight-pos-emv-cryptogram-generator-smoke PRIVATE cxx_std_17)
target_compile_options(omnisight-pos-emv-cryptogram-generator-smoke PRIVATE -Wall -Wextra -Werror)
