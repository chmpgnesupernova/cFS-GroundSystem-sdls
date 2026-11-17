## 실행

<img width="3822" height="2025" alt="스크린샷 2026-11-17 13-27-04" src="https://github.com/user-attachments/assets/2ab9971a-ef7a-419a-a825-d85186221189" />

1. GUI 시스템: GroundSystem.py
2. 프레이밍 및 로깅: sdlp_link.py
3. 네트워크: Subsystem/cmdGui/network_config.py

GroundSystem 에서 SPP 메시지 전송 및 수신.

sdlp_link.py 는 SPP TC/TM 메시지를 프레임으로 encap, 로깅함. 

raw 로그를 보고싶다면 `python3 sdlp_link.py --raw` 옵션 사용. 

GroundSystem 에서 Target IP 를 변경 가능

handcraft 기능 추가: 사용자 정의 SPP 메시지 제작 및 전송 가능.

변경한 결과는 network-config.ini 에 저장

GUI 가 아닌 cmdUtil 사용할 경우 프레이밍 동작 안하니 주의.

## 빌드

libsdls.so 를 빌드, build 디렉토리에 라이브러리 위치

라이브러리에 SDLP-only 옵션 사용 가능

CMakeLists.txt 에서 다음과 같이 설정.
`option(SDLS_SDLP_ONLY "Build SDLP framing only (Security Header/Trailer zero filled)" ON)`

## cFS <--> GS
테스트 완료

<img width="1987" height="547" alt="스크린샷 2026-11-17 19-45-31" src="https://github.com/user-attachments/assets/197f0d30-b602-4ae0-8e6f-0fb7d36b9471" />

cFS -> GS

<img width="2203" height="247" alt="스크린샷 2026-11-17 19-47-00" src="https://github.com/user-attachments/assets/7a5835f6-2a2e-445a-ad2e-d67b502e7659" />

GS -> cFS
