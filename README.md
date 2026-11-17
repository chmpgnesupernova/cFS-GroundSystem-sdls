## 실행

<img width="3822" height="2025" alt="스크린샷 2026-11-17 13-27-04" src="https://github.com/user-attachments/assets/2ab9971a-ef7a-419a-a825-d85186221189" />

GUI 시스템: GroundSystem.py
프레이밍 및 로깅: sdlp_link.py
네트워크: Subsystem/cmdGui/network_config.py

GroundSystem 에서 Target IP 를 변경 가능
변경한 결과는 network-config.ini 에 저장

GUI 가 아닌 cmdUtil 사용할 경우 프레이밍 동작 안하니 주의.

## lib-sdls 빌드
SDLP-only 옵션 사용 가능

CMakeLists.txt 에서 다음과 같이 설정.
`option(SDLS_SDLP_ONLY "Build SDLP framing only (Security Header/Trailer zero filled)" ON)`
