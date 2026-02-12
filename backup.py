import os
import re
import requests
import json
import threading
from datetime import datetime
from collections import defaultdict
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

BACKUP_PATH = './backups'
COOKIE_FILE = 'talkcloud.kakao.com_cookies.txt'
FETCH_COUNT = 100
THREADS_COUNT = 5
MAX_SIZE_BYTES = 5 * 1024 * 1024 * 1024  # 5GB
MAX_RETRIES = 3
TIMEOUT = (10, 60)  # (연결 타임아웃, 읽기 타임아웃) 초

REQ_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36',
    'Accept': 'application/json+javascript'
}

os.makedirs(BACKUP_PATH, exist_ok=True)

# 쿠키 읽기
cookies = {}
with open(COOKIE_FILE, 'r', encoding='utf-8') as file:
    for line in file:
        line = line.strip()
        if line and not line.startswith('#'):
            parts = line.split('\t')
            if len(parts) >= 7:
                cookies[parts[5].strip()] = parts[6].strip()

print(f"쿠키 {len(cookies)}개 로드됨")

# 세션 설정 (연결 재사용 + 자동 재시도)
session = requests.Session()
retry_strategy = Retry(
    total=MAX_RETRIES,
    backoff_factor=1,  # 1초, 2초, 4초 간격으로 재시도
    status_forcelist=[429, 500, 502, 503, 504],
)
adapter = HTTPAdapter(
    max_retries=retry_strategy,
    pool_connections=THREADS_COUNT,
    pool_maxsize=THREADS_COUNT,
)
session.mount('https://', adapter)
session.headers.update(REQ_HEADERS)
session.cookies.update(cookies)


def get_next_seq(folder, date_str):
    """폴더 내 같은 날짜 파일들을 확인해서 다음 순번 반환"""
    if not os.path.exists(folder):
        return 1
    pattern = re.compile(rf'^{date_str}_(\d{{3}})\.')
    max_seq = 0
    for f in os.listdir(folder):
        m = pattern.match(f)
        if m:
            max_seq = max(max_seq, int(m.group(1)))
    return max_seq + 1


def request_list():
    url = f'https://drawer-api.kakao.com/mediaFile/list?verticalType=MEDIA&fetchCount={FETCH_COUNT}&joined=true&direction=ASC'
    response = session.get(url, timeout=TIMEOUT)
    response.raise_for_status()
    return response.json()


def download_item(item, results, index):
    """단일 항목 다운로드 (청크 방식 + 재시도)"""
    chat_id = item['hashedChatId']
    created = datetime.fromtimestamp(int(item['createdAt']) / 1000)
    date_str = created.strftime('%Y%m%d')
    ext = item['extension']
    expected_size = int(item['size'])

    folder = os.path.join(BACKUP_PATH, f"chat_{chat_id[:8]}")
    os.makedirs(folder, exist_ok=True)

    # 순번 결정
    with seq_lock:
        key = (chat_id[:8], date_str)
        seq = seq_counters.get(key, get_next_seq(folder, date_str))
        seq_counters[key] = seq + 1

    filename = f"{date_str}_{seq:03d}.{ext}"
    filepath = os.path.join(folder, filename)

    # 재시도 루프
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = session.get(f"{item['url']}?attach", timeout=TIMEOUT, stream=True)
            if response.status_code != 200:
                raise Exception(f"HTTP {response.status_code}")

            # 청크 방식으로 저장
            downloaded = 0
            with open(filepath, 'wb') as f:
                for chunk in response.iter_content(chunk_size=1024 * 1024):  # 1MB 청크
                    f.write(chunk)
                    downloaded += len(chunk)

            # 크기 검증 (동영상은 메타데이터 size와 실제 다운로드 크기가 달라 0바이트 체크만)
            is_video = item.get('contentType') != 'IMAGE'
            if downloaded == 0:
                os.remove(filepath)
                raise Exception("빈 파일 (0 bytes)")
            if not is_video and downloaded != expected_size:
                os.remove(filepath)
                raise Exception(f"크기 불일치 (expected={expected_size}, actual={downloaded})")

            results[index] = {'success': True, 'id': item['id'], 'path': filepath, 'size': downloaded}
            print(f"  OK  {filepath} ({downloaded:,} bytes)")
            return

        except Exception as e:
            if attempt < MAX_RETRIES:
                print(f"  재시도 {attempt}/{MAX_RETRIES} {filename}: {e}")
            else:
                # 실패한 임시 파일 정리
                if os.path.exists(filepath):
                    os.remove(filepath)
                results[index] = {'success': False, 'id': item['id'], 'error': str(e)}
                print(f"  FAIL {filename}: {e}")


def request_delete(ids):
    data = {"verticalType": "MEDIA", "ids": ids}
    response = session.post(
        'https://drawer-api.kakao.com/mediaFile/delete',
        data=json.dumps(data),
        headers={'Content-Type': 'application/json'},
        timeout=TIMEOUT,
    )
    return response.status_code == 204


# 메인 루프
batch = 0
total_downloaded = 0
total_deleted = 0
total_failed = 0
total_bytes = 0
seq_lock = threading.Lock()
seq_counters = {}

while True:
    batch += 1

    # 용량 제한 체크
    if total_bytes >= MAX_SIZE_BYTES:
        print(f"\n용량 제한 도달 ({total_bytes / (1024**3):.2f} GB). 중단합니다.")
        break

    # 목록 요청
    try:
        file_list = request_list()
    except Exception as e:
        print(f"\n목록 요청 실패: {e}")
        break

    total_count = file_list.get('totalCount', 0)
    items = file_list.get('items', [])

    if total_count == 0 or len(items) == 0:
        print("\n모든 사진 처리 완료!")
        break

    print(f"\n===== 배치 {batch} | 남은 사진: {total_count}개 | 이번 배치: {len(items)}개 | 누적: {total_bytes / (1024**3):.2f} GB =====")

    # 멀티스레드 다운로드
    results = [None] * len(items)
    threads = []

    for i, item in enumerate(items):
        thread = threading.Thread(target=download_item, args=(item, results, i))
        threads.append(thread)
        thread.start()
        if len(threads) >= THREADS_COUNT:
            for t in threads:
                t.join()
            threads = []

    for t in threads:
        t.join()

    # 결과 집계
    success = [r for r in results if r and r['success']]
    failed = [r for r in results if r and not r['success']]

    batch_bytes = sum(r['size'] for r in success)
    total_bytes += batch_bytes
    total_downloaded += len(success)
    total_failed += len(failed)

    print(f"\n  결과: 성공 {len(success)}개 ({batch_bytes / (1024**2):.1f} MB) / 실패 {len(failed)}개")

    if failed:
        print("  실패 항목:")
        for f in failed:
            print(f"    - {f['id']}: {f['error']}")

    # 성공한 파일만 서버에서 삭제
    if success:
        success_ids = [r['id'] for r in success]
        if request_delete(success_ids):
            total_deleted += len(success_ids)
            print(f"  서버 삭제 완료 ({len(success_ids)}개)")
        else:
            print("  서버 삭제 실패! 다음 실행 시 재시도됩니다.")

print(f"\n===== 최종 결과 =====")
print(f"다운로드 성공: {total_downloaded}개 ({total_bytes / (1024**3):.2f} GB)")
print(f"서버 삭제: {total_deleted}개")
print(f"실패: {total_failed}개")
