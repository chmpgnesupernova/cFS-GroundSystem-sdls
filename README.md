![Static Analysis](https://github.com/nasa/cFS-GroundSystem/workflows/Static%20Analysis/badge.svg)
![Format Check](https://github.com/nasa/cFS-GroundSystem/workflows/Format%20Check/badge.svg)

# Core Flight System : Framework : Tool : Ground System

This repository contains NASA's Lab Ground System (cFS-GroundSystem), which is a framework component of the Core Flight System.

This lab application is a non-flight utility ground system to interact with the cFS. It is intended to be located in the `tools/cFS-GroundSystem` subdirectory of a cFS Mission Tree. The Core Flight System is bundled at <https://github.com/nasa/cFS> (which includes cFS-GroundSystem as a submodule), which includes build and execution instructions.

See [Guide-GroundSystem.md](https://github.com/nasa/cFS-GroundSystem/blob/master/Guide-GroundSystem.md) for more information.

## lib_sdls 통합 (SDLP 프레임)

이 저장소의 GroundSystem은 명령과 텔레메트리를 CCSDS Transfer Frame으로 주고받습니다.
`lib_sdls/`의 C 라이브러리(`libsdls.so`)를 Python 공용 모듈 `lib_sdls/sdlp_link.py`가 ctypes로 불러 씁니다.

| 위치 | 하는 일 |
|---|---|
| `Subsystems/cmdGui/MiniCmdUtil.py` | 명령 Space Packet을 TC 프레임으로 감싼 뒤 UDP로 전송. 프레임을 만들 수 없으면 **전송하지 않음** |
| `RoutingService.py` | UDP로 받은 TM 프레임에서 Space Packet들을 꺼내 하나씩 ZeroMQ로 발행. 프레임이 아니면 버림 |
| `lib_sdls/sdlp_link.py` | 위 두 곳이 쓰는 공용 모듈 + 처리 전/후 raw 값 로그 + 모니터 |

GUI(텔레메트리 화면, 명령 화면)는 바뀌지 않았습니다. RoutingService가 예전처럼 Space Packet을 발행하기 때문입니다.

### 빌드

```sh
cmake -S lib_sdls -B lib_sdls/build          # 기본: SDLP-only (wolfSSL 불필요)
cmake --build lib_sdls/build
```

- **SDLP-only (기본, `SDLS_SDLP_ONLY=ON`)**: 프레임 캡슐화/디캡슐화만 합니다. Security Header(14 B)와 Trailer(16 B)는 자리만 차지하고 0으로 채웁니다(수신 시 검사 안 함). SDLS·암호화 코드는 저장소에 남아 있지만 빌드되지 않습니다.
- **SDLS (`-DSDLS_SDLP_ONLY=OFF`)**: SA DB, AES-256-GCM, PQC 키 교환까지 빌드합니다. wolfSSL(`--enable-aesgcm --enable-mlkem --enable-mldsa`)이 필요합니다. `lib_sdls/docs/frame-headers.md` 참고.

### 실행과 모니터

```sh
python3 GroundSystem.py                      # 평소처럼 실행
python3 lib_sdls/sdlp_link.py                # 다른 터미널: TC/TM 처리 전후 raw 값을 실시간으로 표시
python3 lib_sdls/sdlp_link.py --all --dir TC # 지난 로그부터, TC만 / --fail 실패만 / --no-hex 헤더만 / --help
```

모든 TC/TM 처리는 `/tmp/sdlp_link.log`에 한 줄씩 남습니다(시각, 방향, 결과, 프로세스, 처리 전 hex, 처리 후 hex).
환경변수 `SDLP_LOG`로 위치를 바꾸거나 `off`로 끌 수 있고, `SDLS_LIB`로 다른 `libsdls.so`를 지정할 수 있습니다.

### 주의

- 위성(cFS CI_LAB/TO_LAB)도 같은 프레임 형식이어야 합니다: TC는 Bypass 1·SCID 0x003·VCID 0, TM은 1786 B 고정, 양쪽 모두 Security Header 14 B + Trailer 16 B(0).
- TC 프레임 하나에 담을 수 있는 명령 패킷은 최대 989 B입니다.
- `Subsystems/cmdUtil`(C 명령 도구, `*.sh` 스크립트)과 `TlmUDPSender.py`는 프레임을 쓰지 않습니다. 위성/RoutingService가 버리게 되니 GUI로 명령을 보내세요.

## Version History

### Development Build: v3.0.0-rc4+dev7

- Free Address Info to fix resource leak
- Converted most variable,function, and method names into snake_case
- Set new build baseline for cFS-Caelum-rc4: v3.0.0-rc4
- See <https://github.com/nasa/cFS-GroundSystem/pull/201> and <https://github.com/nasa/cFS/pull/390> 


### Development Build: v2.2.0-rc1+dev63

- Add Virtualenv and Pipenv .gitignore support
- Fix doc, comment, and message typos 
- See <https://github.com/nasa/cFS-GroundSystem/pull/195> and <https://github.com/nasa/cFS/pull/348>

### Development Build: v2.2.0-rc1+dev58

- Update tlm for ES Blockstats/memstats and TBL HK
- Assign variables before referencing them
- See <https://github.com/nasa/cFS-GroundSystem/pull/192> and <https://github.com/nasa/cfs/pull/313>

### Development Build: v2.2.0-rc1+dev52

- Add test start command script for cmdUtil
- Implement Coding Standard in CodeQL
- See <https://github.com/nasa/cFS-GroundSystem/pull/183> and <https://github.com/nasa/cFS/pull/270>

### Development Build: v2.2.0-rc1+dev46

- Changes executable command from 'startg' to 'cFS-GroundSystem'
- Changes version to be the version stated in version.py
- Adds executable installation instructions to Guide-GroundSystem.md
- See <https://github.com/nasa/cFS-GroundSystem/pull/178> and <https://github.com/nasa/cFS/pull/248>

### Development Build: v2.2.0-rc1+dev41

- Corrects values in sb and tbl hk-tlm.txt to allow the TBL and SB tlm pages to open.
- Adds a contributing guide that links to the main cFS contributing guide.
- See <https://github.com/nasa/cfs-groundsystem/pull/171>

### Development Build: v2.2.0-rc1+dev33

- Fix #163, Add Testing Tools to the Security Policy
- See <https://github.com/nasa/cfs-groundsystem/pull/167>

### Development Build: v2.2.0-rc1+dev18

- Documentation: Add `Security.md` with instructions to report vulnerabilities
- **Breaking change**, CmdUtil, Rounds header up to match <https://github.com/nasa/cFE/pull/1077>
- **Breaking change**, GUI, Rounds header up to match <https://github.com/nasa/cFE/pull/1077>
- See <https://github.com/nasa/cFS-GroundSystem/pull/150>

### Development Build: v2.2.0-rc1+dev11

- Updated CHeaderParser.py to address specific issues.
- See <https://github.com/nasa/cFS-GroundSystem/pull/135>

### Development Build: v2.2.0-rc1+dev8

- Replaces old code that caused a cast-align warning when strict. Refactored and removed unnecessary code while also following recommended model for getaddrinfo. Removed old windows support/defines/etc (likely not tested for years, no longer supported).
- Reduce the size of the strncpy so that it ensures there's a null byte at the end of the string buffer.
- See <https://github.com/nasa/cFS-GroundSystem/pull/133>

### Development Build: v2.2.0+dev2

 - Fixes multiple typos
- See <https://github.com/nasa/cFS-GroundSystem/pull/127>

### Development Build: v2.1.0+dev85

- Remove unused code/packages to fix LGTM warnings
- See <https://github.com/nasa/cFS-GroundSystem/pull/120>

### Development Build: v2.1.0+dev76

- Fixes more lgtm warnings
- Allows users to change the byte offsets for sending commands and parsing telemetry, to support different header versions or other implementations of cFS
- Adds a file to store version information and reports version upon ground-system startup.
- See <https://github.com/nasa/cFS-GroundSystem/pull/109>

### Development Build: 2.1.12

- Change all individual UI elements to table widgets. Update backend code accordingly
- Temporary fix for implicit declaration of endian functions on some systems (RH/CentOs). No build errors on CentOS
- See <https://github.com/nasa/cFS-GroundSystem/pull/107>

### Development Build: 2.1.11

- Default behavior is the same except adds checksum and doesn't actually require fields. Adds all the packet fields, overrides, more supported data types, etc.
- See <https://github.com/nasa/cFS-GroundSystem/pull/101>

### Development Build: 2.1.10

- Change documentation for table loading guide to markdown
- See <https://github.com/nasa/cFS-GroundSystem/pull/94>

### Development Build: 2.1.9

- Upgrading PyQt4 to PyQt5 and includes a lot of cleanup/refactoring, and changes to the GUI itself
- See <https://github.com/nasa/cFS-GroundSystem/pull/90>

### Development Build: 2.1.8

- No warnings when building with GCC9
- Event messages now display both Event type and ID.
- See <https://github.com/nasa/cFS-GroundSystem/pull/86>

### Development Build: 2.1.7

- Commands and Telemetry definitions now match code
- See <https://github.com/nasa/cFS-GroundSystem/pull/74>

### Development Build: 2.1.6

- Cmd code (and checksum) are always in the same place
- See <https://github.com/nasa/cFS-GroundSystem/pull/69>

### Development Build: 2.1.5

- Updated build instructions for Python 3
- See <https://github.com/nasa/cFS-GroundSystem/pull/64>

### Development Build: 2.1.4

- Finish conversion to python 3
- cmdutil now accepts --word as alias to --long
- See <https://github.com/nasa/cFS-GroundSystem/pull/54>

### Development Build: 2.1.3

- Minor updates to work with python 3
- No longer compatible with python 2.7
- Note issue #50 is to update the related documentation
- See <https://github.com/nasa/cFS-GroundSystem/pull/47>

### Development Build: 2.1.2

- Minor updates (see <https://github.com/nasa/cFS-GroundSystem/pull/39>)

### Development Build: 2.1.1

- Minor updates (see <https://github.com/nasa/cFS-GroundSystem/pull/36>)

### **_OFFICIAL RELEASE 2.1.0 - Aquila_**

- Minor updates (see <https://github.com/nasa/cFS-GroundSystem/pull/26>)
- Released as part of cFE 6.7.0, Apache 2.0

### **_OFFICIAL RELEASE 2.0.90a_**

- Released as part of cFE 6.6.0a, Apache 2.0

## Known issues

As a lab application, extensive testing is not performed prior to release and only minimal functionality is included.

## Getting Help

For best results, submit issues:questions or issues:help wanted requests at <https://github.com/nasa/cFS>.

Official cFS page: <http://cfs.gsfc.nasa.gov>
