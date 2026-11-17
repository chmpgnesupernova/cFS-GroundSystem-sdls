## 실행

<img width="3822" height="2025" alt="스크린샷 2026-11-17 13-27-04" src="https://github.com/user-attachments/assets/2ab9971a-ef7a-419a-a825-d85186221189" />

1. GUI 시스템: GroundSystem.py
2. 프레이밍 및 로깅: sdlp_link.py
3. 네트워크: Subsystem/cmdGui/network_config.py

GroundSystem 에서 SPP 메시지 전송 및 수신.

sdlp_link.py 는 SPP TC/TM 메시지를 프레임으로 encap, 로깅함. 

raw 로그를 보고싶다면 `python3 sdlp_link.py --raw` 옵션 사용. 

GroundSystem 에서 Target IP 를 변경 가능

변경한 결과는 network-config.ini 에 저장

GUI 가 아닌 cmdUtil 사용할 경우 프레이밍 동작 안하니 주의.

## 빌드

libsdls.so 를 빌드, build 디렉토리에 라이브러리 위치

라이브러리에 SDLP-only 옵션 사용 가능

CMakeLists.txt 에서 다음과 같이 설정.
`option(SDLS_SDLP_ONLY "Build SDLP framing only (Security Header/Trailer zero filled)" ON)`
