# 카카오톡 톡서랍 일괄 백업

톡서랍(talkcloud.kakao.com)에 저장된 사진/동영상/파일/링크를 오래된 순서부터 로컬에 백업합니다.

## 스크립트

| 파일 | 설명 |
|------|------|
| `backup.py` | 다운로드 후 서버에서 삭제 (백업 + 정리) |
| `download_only.py` | 다운로드만 수행, 서버 삭제 없음 |

두 스크립트 모두 타입 인자를 지원합니다:

```bash
python backup.py              # MEDIA (사진/동영상, 기본값)
python backup.py FILE         # FILE (문서/파일)
python backup.py LINK         # LINK (텍스트/링크)

python download_only.py       # MEDIA (기본값)
python download_only.py FILE  # FILE
python download_only.py LINK  # LINK
```

### 지원 타입

| 타입 | API verticalType | 내용 | 파일명 규칙 |
|------|-----------------|------|------------|
| `MEDIA` | MEDIA | 사진, 동영상 (jpg, mp4 등) | `YYYYMMDD_NNN.확장자` |
| `FILE` | FILE | 문서, 파일 (pdf, xlsx 등) | 원본 파일명 사용 |
| `LINK` | LINK | 텍스트, 링크 (.txt) | `YYYYMMDD_NNN.txt` |

### backup.py

- 오래된 파일부터 100개씩 가져와 다운로드
- 다운로드 성공한 파일만 서버에서 삭제
- 삭제된 파일은 다음 배치에서 자동으로 건너뛰므로 cursor 없이 항상 첫 페이지를 요청
- 실행할 때마다 설정된 용량(기본 5GB)까지 처리 후 중단

### download_only.py

- 서버에서 삭제하지 않으므로 cursor 기반 페이징으로 다음 페이지를 넘김
- **커서 자동 저장/재개**: 타입별로 커서 파일(`download_cursor_{TYPE}.txt`)을 저장하여 다음 실행 시 이어서 진행
- 서버 데이터를 보존하면서 로컬 백업만 만들고 싶을 때 사용
- 실행할 때마다 설정된 용량(기본 5GB)까지 처리 후 중단
- 모든 파일 처리 완료 시 커서 파일이 삭제되어 다음 실행 시 처음부터 시작

### 다운로드 이력 (download_history.csv)

두 스크립트는 `backups/download_history.csv`에 다운로드 이력을 공유합니다. 모든 타입의 이력이 하나의 CSV에 기록되며, 이력에 있는 파일은 중복 다운로드하지 않습니다.

```csv
id,chatId,date,filename,size,contentType,status,downloadedAt
62177e5fa9b5ac1f0e84d5ac,96e73c34,20200611,20200611_001.jpg,21628,IMAGE,OK,2026-02-12 15:30:00
62177f0d3d7b772940d52d04,977e1f99,20200831,보고서_최종.pdf,97797,FILE,OK,2026-02-12 16:00:00
62177fc4d700ca293f9bfa25,,,,0,,FAIL: 빈 파일 (0 bytes),2026-02-12 15:30:05
```

| 컬럼 | 설명 |
|------|------|
| `id` | 카카오 API 파일 고유 ID |
| `chatId` | 채팅방 해시 앞 8자리 |
| `date` | 파일 생성일 (YYYYMMDD) |
| `filename` | 저장된 파일명 |
| `size` | 실제 다운로드 크기 (bytes) |
| `contentType` | IMAGE, VIDEO, FILE, TEXT 등 |
| `status` | OK 또는 FAIL: 사유 |
| `downloadedAt` | 처리 시각 |

성공(OK)과 실패(FAIL) 모두 기록됩니다. 이력에 있는 파일은 성공/실패 관계없이 재시도하지 않습니다.

## 사전 준비

1. Python 3 설치
2. requests 라이브러리 설치
   ```bash
   pip install requests
   ```
3. [Get cookies.txt locally](https://chrome.google.com/webstore/detail/get-cookiestxt-locally/cclelndahbckbenkjhflpdbgdldlbecc/) Chrome 확장 프로그램 설치
4. 브라우저에서 https://talkcloud.kakao.com 접속 및 카카오 계정으로 로그인
5. 확장 프로그램으로 쿠키 export → 파일명을 `talkcloud.kakao.com_cookies.txt`로 저장하여 프로젝트 폴더에 배치

![쿠키 내보내기](./cookie.PNG)

## 실행

```bash
# 사진/동영상 백업 + 서버 삭제
python backup.py

# 문서/파일 백업 + 서버 삭제
python backup.py FILE

# 텍스트/링크 백업 + 서버 삭제
python backup.py LINK

# 다운로드만 (서버 삭제 없음)
python download_only.py
python download_only.py FILE
python download_only.py LINK
```

## 설정

각 스크립트 상단에서 수정 가능:

| 변수 | 기본값 | 설명 |
|------|--------|------|
| `BACKUP_PATH` | `./backups` | 다운로드 저장 경로 |
| `COOKIE_FILE` | `talkcloud.kakao.com_cookies.txt` | 쿠키 파일 경로 |
| `MAX_SIZE_BYTES` | `5 * 1024 * 1024 * 1024` (5GB) | 1회 실행당 최대 다운로드 용량 |
| `FETCH_COUNT` | `100` | API 요청 시 한 번에 가져올 파일 수 |
| `THREADS_COUNT` | `5` | 동시 다운로드 스레드 수 |
| `MAX_RETRIES` | `3` | 다운로드 실패 시 재시도 횟수 (1초 → 2초 → 4초 간격) |
| `CONNECT_TIMEOUT` | `3` | 연결 타임아웃 (초) |
| `READ_TIMEOUT_BASE` | `3` | 읽기 타임아웃 기본값 (초) |
| `READ_TIMEOUT_PER_MB` | `2` | MB당 추가 읽기 타임아웃 (초). 실제 타임아웃 = 기본값 + 파일크기(MB) × 이 값 |
| `MAX_COUNT` | `None` | download_only.py 전용. 다운로드 개수 제한 (None이면 제한 없음) |

## 백업 폴더 구조

타입별로 폴더가 분리되며, 각 타입 안에서 채팅방별 폴더로 분류됩니다.

```
backups/
├── chat_96e73c34/              # MEDIA (사진/동영상)
│   ├── 20200611_001.jpg
│   ├── 20200611_002.jpg
│   ├── 20200624_001.mp4
│   └── 20200624_002.jpg
├── chat_99c8602a/
│   └── 20200616_001.jpg
├── files/                      # FILE (문서/파일)
│   ├── chat_977e1f99/
│   │   ├── 보고서_최종.pdf
│   │   └── 보고서_최종 (1).pdf   # 동명 파일 자동 번호 부여
│   └── chat_484c75f2/
│       └── 회의록.xlsx
├── links/                      # LINK (텍스트/링크)
│   └── chat_bf889899/
│       ├── 20200703_001.txt
│       └── 20200703_002.txt
├── download_history.csv        # 공유 이력 (모든 타입)
├── download_cursor_MEDIA.txt   # 타입별 커서 (download_only.py용)
├── download_cursor_FILE.txt
└── download_cursor_LINK.txt
```

- MEDIA 폴더명: `chat_해시앞8자리` (기존과 동일)
- FILE 폴더: `files/chat_해시앞8자리/원본파일명`
- LINK 폴더: `links/chat_해시앞8자리/YYYYMMDD_순번.txt`
- 채팅방 이름이나 참여자 이름은 API에서 제공하지 않아 해시로만 구분됩니다
- FILE 타입은 API가 제공하는 원본 파일명(`originalFileName`)을 사용합니다. 같은 이름의 파일이 있으면 `(1)`, `(2)` 등이 붙습니다
- MEDIA/LINK 타입은 원본 파일명이 없어 `날짜_순번` 형식을 사용합니다

## 사용 사례

### 1. 모든 타입 한 번에 백업 + 서버 정리
```bash
python backup.py          # 사진/동영상
python backup.py FILE     # 문서/파일
python backup.py LINK     # 텍스트/링크
```

### 2. 서버 데이터를 보존하면서 로컬에만 백업
```bash
python download_only.py
python download_only.py FILE
python download_only.py LINK
```

### 3. 먼저 다운로드 확인 후 서버 정리
```bash
python download_only.py FILE  # 1단계: 다운로드만
# 로컬에서 파일 확인 후...
python backup.py FILE         # 2단계: 이미 받은 파일은 다운로드 생략, 서버에서 삭제만 진행
```

### 4. 용량 제한을 바꿔서 실행
```python
# backup.py 또는 download_only.py 상단 수정
MAX_SIZE_BYTES = 10 * 1024 * 1024 * 1024  # 10GB로 변경
```

## 안전장치

- **0바이트 검증**: 다운로드된 파일이 0바이트인지만 체크합니다. 카카오 서버에서 파일 변환/리사이즈로 인해 메타데이터의 size와 실제 다운로드 크기가 다를 수 있어 크기 비교 검증은 하지 않습니다.
- **성공 건만 삭제** (backup.py): 다운로드에 성공한 파일만 서버에서 삭제합니다. 실패한 파일은 서버에 그대로 남아 다음 실행 시 재시도됩니다.
- **자동 재시도**: 다운로드 실패 시 최대 3회 재시도합니다 (1초, 2초, 4초 간격의 지수 백오프).
- **청크 다운로드**: 대용량 파일을 1MB 단위로 나눠 저장하여 메모리 부족 및 불완전 다운로드를 방지합니다.
- **세션 재사용**: TCP 연결 풀링으로 매번 새 연결을 맺지 않아 안정성과 속도가 향상됩니다.
- **용량 제한**: 실제 다운로드된 바이트 기준으로 설정된 용량에 도달하면 자동 중단됩니다. 배치(100개) 단위로 체크하므로 최대 1배치분만큼 초과할 수 있습니다.
- **재실행 안전**: 폴더 내 기존 파일의 순번을 확인하고 이어서 부여합니다.
- **다운로드 이력**: `download_history.csv`에 성공/실패 모두 기록하여 중복 시도를 방지합니다. 계속 실패하는 파일도 이력에 기록되어 다음 실행 시 스킵됩니다. `backup.py`에서는 이미 다운로드된 파일은 다운로드를 생략하고 서버 삭제만 진행합니다.
- **커서 재개** (download_only.py): 타입별로 마지막 처리 위치를 커서 파일에 저장합니다. 중단 후 재실행 시 처음부터 다시 스캔하지 않고 이어서 진행합니다.
- **동적 타임아웃**: 파일 크기에 비례하여 읽기 타임아웃을 자동 조절합니다 (기본 3초 + MB당 2초). 작은 파일은 빠르게 실패 판정하고, 대용량 파일은 충분한 시간을 줍니다.
- **파일명 충돌 방지** (FILE 타입): 원본 파일명이 같은 파일이 여러 개일 경우 자동으로 `(1)`, `(2)` 등을 붙여 구분합니다.

## 알려진 이슈

- **파일 크기 불일치**: 카카오 API 메타데이터의 `size` 필드와 실제 다운로드 크기가 일부 파일(동영상, 일부 이미지)에서 일치하지 않습니다. 서버 내부 변환/리사이즈로 추정되며, 이 때문에 크기 비교 검증 대신 0바이트 체크만 수행합니다.
- **채팅방 식별 불가**: API가 채팅방 이름이나 참여자 정보를 제공하지 않아 해시값(`hashedChatId` 앞 8자리)으로만 구분됩니다. 어떤 채팅방인지 확인하려면 폴더 안의 파일 내용으로 직접 판단해야 합니다.
- **MEDIA/LINK 원본 파일명 없음**: MEDIA와 LINK 타입은 API에 원본 파일명이 포함되지 않아 `날짜_순번` 형식으로 저장됩니다. FILE 타입만 원본 파일명을 사용합니다.

## 참고

- 쿠키는 일정 시간이 지나면 만료됩니다. 인증 오류가 발생하면 쿠키를 다시 export 해주세요.
- `drive.kakao.com_cookies.txt`는 구버전 쿠키 파일입니다. 현재는 `talkcloud.kakao.com_cookies.txt`를 사용합니다.
- API 엔드포인트는 `drawer-api.kakao.com`이며, CDN은 `drawer-cdn.kakao.com`입니다. `talkcloud.kakao.com` 쿠키로 인증됩니다.
