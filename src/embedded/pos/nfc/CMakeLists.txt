# Case 7 POS NFC reader drivers CMake fragment (OP-2040 PN532, OP-2049 PN5180).
#
# Include this fragment from the embedded platform CMake entry point. Cross
# compilation is supplied by the caller through the platform toolchain file.

# ── NXP PN532 NFC reader (OP-2040) ──────────────────────────────────

add_library(omnisight-pos-nfc-pn532-driver STATIC
  ${CMAKE_CURRENT_LIST_DIR}/pn532-driver.cpp
)
target_include_directories(omnisight-pos-nfc-pn532-driver PUBLIC
  ${CMAKE_CURRENT_LIST_DIR}
)
target_compile_features(omnisight-pos-nfc-pn532-driver PUBLIC cxx_std_17)
target_compile_options(omnisight-pos-nfc-pn532-driver PRIVATE -Wall -Wextra -Werror)

add_executable(omnisight-pos-nfc-pn532-driver-smoke
  ${CMAKE_CURRENT_LIST_DIR}/pn532-driver.cpp
)
target_compile_definitions(omnisight-pos-nfc-pn532-driver-smoke PRIVATE
  OMNISIGHT_POS_NFC_PN532_DRIVER_SMOKE_MAIN=1
)
target_compile_features(omnisight-pos-nfc-pn532-driver-smoke PRIVATE cxx_std_17)
target_compile_options(omnisight-pos-nfc-pn532-driver-smoke PRIVATE -Wall -Wextra -Werror)

# ── NXP PN5180 high-perf NFC reader (OP-2049) ───────────────────────

add_library(omnisight-pos-nfc-pn5180-driver STATIC
  ${CMAKE_CURRENT_LIST_DIR}/pn5180-driver.cpp
)
target_include_directories(omnisight-pos-nfc-pn5180-driver PUBLIC
  ${CMAKE_CURRENT_LIST_DIR}
)
target_compile_features(omnisight-pos-nfc-pn5180-driver PUBLIC cxx_std_17)
target_compile_options(omnisight-pos-nfc-pn5180-driver PRIVATE -Wall -Wextra -Werror)

add_executable(omnisight-pos-nfc-pn5180-driver-smoke
  ${CMAKE_CURRENT_LIST_DIR}/pn5180-driver.cpp
)
target_compile_definitions(omnisight-pos-nfc-pn5180-driver-smoke PRIVATE
  OMNISIGHT_POS_NFC_PN5180_DRIVER_SMOKE_MAIN=1
)
target_compile_features(omnisight-pos-nfc-pn5180-driver-smoke PRIVATE cxx_std_17)
target_compile_options(omnisight-pos-nfc-pn5180-driver-smoke PRIVATE -Wall -Wextra -Werror)
