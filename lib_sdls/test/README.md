# libsdls.so Python 테스트

Python ground system이 ctypes로 `libsdls.so`를 불러 쓰는 방식 그대로 지상국 SDLP/SDLS/EP를 검증합니다.
위성(cFS) 쪽 코드를 Python으로 옮긴 참조 구현과 바이트 단위로 왕복이 맞는지 확인합니다.
위성 쪽 암호 연산(AES-GCM, ML-KEM, ML-DSA)은 같은 wolfCrypt 백엔드로 빌드한 테스트 전용
`libsdls_satsim.so`가 맡고, 프레임 레이아웃과 AAD 규칙은 Python 참조 구현이 독립적으로 만듭니다.

## 파일

| 파일 | 내용 |
|---|---|
| `sdls_binding.py` | ctypes 바인딩 (함수 시그니처, C 헤더 상수 사본, 예외 래퍼). ground system에서 그대로 가져다 써도 됨 |
| `sat_reference.py` | 위성 쪽 `TC_SDLP_myExtractPacket`, `TM_SDLP_myForwardPacket` 등의 Python 포팅. SA01/SA11 암호화 포함 (`SatCrypto`로 satsim 호출). `SatTmSegmentingFramer`는 현재 to_lab(476f4dc)의 패킷 분할 프레이밍과 바이트 단위로 같음 |
| `satsim/satsim.c` | 테스트 전용 위성 암호 라이브러리 `libsdls_satsim.so` 소스 |
| `test_sdls.py` | SDLS 프레임 보호: TC 암호화를 위성이 복호화, 바이트 변조 거절, MC Frame Count 비인증, 위조 프레임이 ARSN을 못 미는지, 실패 시 데이터 소거. `cryptography`가 있으면 독립 구현으로 교차 검증 |
| `test_sdlp_only.py` | SDLP-only 빌드: TC는 Primary Header + SPP만(Security Header·MAC 없음), TM도 Primary Header + 패킷만(Security Header·MAC 없음), SPI 무시, SA DB 없이 동작, 프레임 검사 유지 (SDLS 빌드에서는 건너뜀) |
| `test_sdlp_link.py` | 공용 모듈 `sdlp_link.py`: TC/TM 처리와 로그 줄 내용, 실패 시 예외·로그, 라이브러리 없음, 로그 끄기·쓰기 실패, 여러 프로세스 동시 기록, 모니터 출력·필터·따라 읽기 |
| `test_groundsystem.py` | 실제 `MiniCmdUtil.py`와 `RoutingService.py`를 UDP로 실행: 명령이 TC 프레임으로 나가는지, 프레임을 못 만들면 안 보내는지, TM 프레임의 패킷이 하나씩 원래 형식으로 발행되는지 (PyQt5·zmq는 가짜로 대체) |
| `test_ep.py` | 키 교환: 위성 ek → 지상 캡슐화 → 위성 역캡슐화 → 양방향 보호 통신, 키 재교환, 변조된 ct, 잘못된 ek, ML-DSA 서명 검증, 프로세스 간 ct·서명키 공유 |
| `test_tc_sdlp.py` | `TC_SDLP_myInit`, `TC_SDLP_myBuildFrame` 테스트 |
| `test_tm_sdlp.py` | `TM_SDLP_myExtractPacket` 테스트 |
| `test_tm_stream.py` | TM 수신 스트림(`TM_SDLP_myRx*`, `sdlp_link.decapsulate_tm`): 프레임 경계를 넘는 패킷 재조립(FHP), 프레임보다 긴 패킷(FHP 0x7FF), 프레임 경계에 걸린 헤더, 다음 프레임으로 넘어간 짧은 idle, 프레임 손실(MC Frame Count) 시 끊긴 패킷만 버림, 송신자별 스트림, 잘못된 FHP 거절, 깨진 헤더 후 재동기 |
| `test_file_rx.py` | `file_rx.py`: FILE_DL 세그먼트를 프레임 재조립으로 받아 파일로 저장, 손실 후 재전송분으로 완성, DestTag·MID 필터, CRC 불일치, END 손실, 경로 조작 방지, 명령(ADD_DEST·RESEND·DONE·REMOVE_DEST) 바이트, UDP로 돌린 독립 수신국(main) |
| `test_file_relay.py` | File 창: 명령 창(위 업링크 OUTPUT_ENABLE·START·DATA·END·STATUS·CANCEL, 아래 다운링크 Filename·DL_START 한 행과 그 아래 ADD_DEST·DONE·REMOVE_DEST·RESEND)의 버튼이 UDP로 내보내는 TC, 상태의 빠진 구간만 DATA로 재전송, 받은 파일의 CRC로 DONE, 텔레메트리 창(위 Transmit 0x08C2, 아래 Receive 0x08C0·0x08C3, DestTag로 거름)의 표시·파일 저장·상태 파일. PyQt5·zmq는 가짜로 대체 |
| `test_msn_gui.py` | MSN App 명령 페이지(NOOP·RESET_COUNTERS·SET_PARAM·SET_STARTUP·RUN_DIAG·GET_APP_STATUS·READ_TEMP)가 Parameter 창 입력으로 만드는 패킷이 msn_app/README.md의 예시 패킷·길이와 같은지, MSN 텔레메트리 페이지 4개(HK·TEMP·DIAG·APP_STATUS)가 TLM 헤더 오프셋 4에서 값을 올바로 표시하는지. PyQt5·zmq는 가짜로 대체 |
| `test_gs_instance.py` | 한 PC에 Ground System 여러 개(`GroundSystem.py --instance <TM 포트>`): 포트·zmq 주소·오프셋 파일·로그·File Relay 상태 파일·SA DB가 포트를 따라가는지, 기본(1235)은 예전 이름 그대로인지, RoutingService 두 개가 동시에 도는지, 포트가 이미 쓰이면 안내 후 종료하는지 |
| `e2e_relay.sh` | 실제 cFS를 띄운 릴레이 종단 시험 (unittest가 아님, 직접 실행). 지상국 A = 위성 저장소의 `file_dl_test/relay_ground_a.py`, 지상국 B = `file_rx.py`. A 업링크 → 위성 저장·CRC 검증 → B로 분할 프레임 다운링크 → B 저장·RESEND·DONE → 원본 비교, 위성 이벤트 확인. `--ul-loss`, `--dl-loss`로 손실 시험 |
| `frame_view.py` | 완성된 TC/TM 프레임(Primary + Security Header + Data + Trailer)을 필드별로 출력. `python3 test/frame_view.py [--hex]` |
| `test_frame_view.py` | `frame_view.py` 디코더 검증 + 실행 확인 |
| `test_sechdr.py` | SDLS Security Header (`SECHDR_my*`) 테스트. TC 헤더 바이트(SPI BE + IV), TM 헤더 SPI/SA/ARSN 검사 |
| `test_sa_shm.py` | 공유메모리 SA/Key DB (`SA_my*`) 테스트. 상태 전이, IV 발급, ARSN 윈도우, 자식 프로세스(spawn)로 프로세스 간 공유와 동시 IV 발급 검증 |

표준 라이브러리(`unittest`, `ctypes`)만 필요합니다. Python `cryptography` 패키지가 있으면 AES-GCM 독립 교차 검증 테스트도 돌고, 없으면 그 테스트만 건너뜁니다.

## 빌드 모드

기본 빌드는 SDLP-only(`SDLS_SDLP_ONLY=ON`)입니다. 이때 SA/SDLS/EP가 필요한 테스트 클래스는 스스로 건너뛰고,
SDLS 빌드(`-DSDLS_SDLP_ONLY=OFF`)에서는 SDLP-only 전용 테스트가 건너뜁니다. 두 빌드 모두 같은 명령으로 돌립니다.

## 실행

wolfSSL이 `--enable-aesgcm --enable-mlkem --enable-mldsa`로 빌드돼 있어야 합니다.

```sh
# wolfSSL을 시스템이 아닌 곳에 설치했다면 -DWOLFSSL_ROOT=/opt/wolfssl 또는 PKG_CONFIG_PATH 지정
cmake -S . -B build
cmake --build build

# 방법 1: ctest (빌드한 라이브러리 경로를 자동으로 넘김)
ctest --test-dir build --output-on-failure

# 방법 2: 직접 실행 (기본으로 build/libsdls.so를 찾음)
python3 -m unittest discover -s test -v

# 다른 위치의 라이브러리를 테스트할 때 (satsim은 기본으로 libsdls.so 옆에서 찾음)
SDLS_LIB=/path/to/libsdls.so SDLS_SATSIM_LIB=/path/to/libsdls_satsim.so python3 -m unittest discover -s test -v
```

## 주의

- ctypes는 `#define`을 읽지 못하므로 `sdls_binding.py`의 상수는 `include/` 헤더 값을 복사한 것입니다. 헤더를 바꾸면 같이 고쳐야 합니다.
- `sat_reference.py`는 위성 쪽 코드가 바뀌면 같이 맞춰야 합니다. 이 파일이 지상국 라이브러리의 정답 기준입니다.
- TC 쪽은 라이브러리 안에 정적 상태(초기화 여부, Frame Sequence Number, SA DB 매핑)가 있어서, Init/`SA_myOpen` 전 호출 테스트는 라이브러리 사본을 따로 로드해서 확인합니다.
- TC/TM 프레임 처리는 Security Header(SPI 2 + IV 12)를 공유메모리 SA DB로 처리하므로, TC/TM 테스트도 테스트마다 새 SA DB를 엽니다. `SatTmFramer(spi=..., key=...)`로 위성이 쓸 SA와 세션키를 고릅니다(기본 SA10, 평문).
- TM 프레임은 SA11이면 라이브러리가 제자리에서 복호화하므로, 바인딩(`tm_extract`)은 받은 바이트를 쓰기 가능한 버퍼로 복사해서 넘깁니다. 반환값의 SPI로 SA10(EP PDU)과 SA11(보호된 텔레메트리)을 구분합니다.
- SA 테스트는 테스트마다 `/sdls_test_<pid>_<n>` 이름의 공유메모리를 만들고 끝나면 지웁니다. 기본 이름(`/sdls_sa_db`)을 쓰는 실행 중인 ground system에는 영향을 주지 않습니다.
