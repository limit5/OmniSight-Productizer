# Cases 6+7 UAV RF link CMake fragment (OP-2039 LoRaWAN framing).
#
# Include this fragment from the embedded platform CMake entry point. Cross
# compilation is supplied by the caller through the platform toolchain file.

add_library(omnisight-uav-rf-lora-packet-framing STATIC
  ${CMAKE_CURRENT_LIST_DIR}/lora-packet-framing.cpp
)
target_include_directories(omnisight-uav-rf-lora-packet-framing PUBLIC
  ${CMAKE_CURRENT_LIST_DIR}
)
target_compile_features(omnisight-uav-rf-lora-packet-framing PUBLIC cxx_std_17)
target_compile_options(omnisight-uav-rf-lora-packet-framing PRIVATE -Wall -Wextra -Werror)

add_executable(omnisight-uav-rf-lora-packet-framing-smoke
  ${CMAKE_CURRENT_LIST_DIR}/lora-packet-framing.cpp
)
target_compile_definitions(omnisight-uav-rf-lora-packet-framing-smoke PRIVATE
  OMNISIGHT_UAV_RF_LORA_PACKET_FRAMING_SMOKE_MAIN=1
)
target_compile_features(omnisight-uav-rf-lora-packet-framing-smoke PRIVATE cxx_std_17)
target_compile_options(omnisight-uav-rf-lora-packet-framing-smoke PRIVATE -Wall -Wextra -Werror)
