import os
import re
import csv
import requests
import json
import threading
from datetime import datetime
from collections import defaultdict
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

BACKUP_PATH = './backups'
COOKIE_FILE = 'talkcloud.kakao.com_cookies.txt'
HISTORY_FILE = os.path.join(BACKUP_PATH, 'download_history.csv')
FETCH_COUNT = 100
THREADS_COUNT = 5
MAX_SIZE_BYTES = 5 * 1024 * 1024 * 1024  # 5GB
MAX_RETRIES = 3
TIMEOUT = (10, 60)  # (연결 타임아웃, 읽기 타임아웃) 초

CSV_FIELDS = ['id', 'chatId', 'date', 'filename', 'size', 'contentType', 'downloadedAt']

REQ_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36',
    'Accept': 'application/json+javascript'
}

os.makedirs(BACKUP_PATH, exist_ok=True)

# 다운로드 이력 로드
downloaded_ids = set()
if os.path.exists(HISTORY_FILE):
    with open(HISTORY_FILE, 'r', encoding='utf-8', newline='') as f:
        reader = csv.DictReader(f)
        for row in reader:
            downloaded_ids.add(row['id'])
else:
    with open(HISTORY_FILE, 'w', encoding='utf-8', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        writer.writeheader()
print(f"다운로드 이력: {len(downloaded_ids)}개 로드됨")

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
    backoff_factor=1,
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
    if not os.path.exists(folder):
        return 1
    pattern = re.compile(rf'^{date_str}_(\d{{3}})\.')
    max_seq = 0
    for f in os.listdir(folder):
        m = pattern.match(f)
        if m:
            max_seq = max(max_seq, int(m.group(1)))
    return max_seq + 1


def request_list(cursor=None):
    url = f'https://drawer-api.kakao.com/mediaFile/list?verticalType=MEDIA&fetchCount={FETCH_COUNT}&joined=true&direction=ASC'
    if cursor:
        url += f'&cursor={cursor}'
    response = session.get(url, timeout=TIMEOUT)
    response.raise_for_status()
    return response.json()


def download_item(item, results, index):
    chat_id = item['hashedChatId'][:8]
    created = datetime.fromtimestamp(int(item['createdAt']) / 1000)
    date_str = created.strftime('%Y%m%d')
    ext = item['extension']
    expected_size = int(item['size'])
    content_type = item.get('contentType', '')

    folder = os.path.join(BACKUP_PATH, f"chat_{chat_id}")
    os.makedirs(folder, exist_ok=True)

    with seq_lock:
        key = (chat_id, date_str)
        seq = seq_counters.get(key, get_next_seq(folder, date_str))
        seq_counters[key] = seq + 1

    filename = f"{date_str}_{seq:03d}.{ext}"
    filepath = os.path.join(folder, filename)

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = session.get(f"{item['url']}?attach", timeout=TIMEOUT, stream=True)
            if response.status_code != 200:
                raise Exception(f"HTTP {response.status_code}")

            downloaded = 0
            with open(filepath, 'wb') as f:
                for chunk in response.iter_content(chunk_size=1024 * 1024):
                    f.write(chunk)
                    downloaded += len(chunk)

            is_video = content_type != 'IMAGE'
            if downloaded == 0:
                os.remove(filepath)
                raise Exception("빈 파일 (0 bytes)")
            if not is_video and downloaded != expected_size:
                os.remove(filepath)
                raise Exception(f"크기 불일치 (expected={expected_size}, actual={downloaded})")

            results[index] = {
                'success': True, 'id': item['id'], 'path': filepath, 'size': downloaded,
                'chatId': chat_id, 'date': date_str, 'filename': filename, 'contentType': content_type,
            }
            print(f"  OK  {filepath} ({downloaded:,} bytes)")
            return

        except Exception as e:
            if attempt < MAX_RETRIES:
                print(f"  재시도 {attempt}/{MAX_RETRIES} {filename}: {e}")
            else:
                if os.path.exists(filepath):
                    os.remove(filepath)
                results[index] = {'success': False, 'id': item['id'], 'error': str(e)}
                print(f"  FAIL {filename}: {e}")


def save_history(success_results):
    """성공한 다운로드를 CSV에 추가"""
    with open(HISTORY_FILE, 'a', encoding='utf-8', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        for r in success_results:
            writer.writerow({
                'id': r['id'],
                'chatId': r['chatId'],
                'date': r['date'],
                'filename': r['filename'],
                'size': r['size'],
                'contentType': r['contentType'],
                'downloadedAt': now,
            })
            downloaded_ids.add(r['id'])


# 메인 루프
batch = 0
total_downloaded = 0
total_skipped = 0
total_failed = 0
total_bytes = 0
cursor = None
seq_lock = threading.Lock()
seq_counters = {}

while True:
    batch += 1

    if total_bytes >= MAX_SIZE_BYTES:
        print(f"\n용량 제한 도달 ({total_bytes / (1024**3):.2f} GB). 중단합니다.")
        break

    try:
        file_list = request_list(cursor)
    except Exception as e:
        print(f"\n목록 요청 실패: {e}")
        break

    total_count = file_list.get('totalCount', 0)
    items = file_list.get('items', [])
    has_more = file_list.get('hasMore', False)

    if total_count == 0 or len(items) == 0:
        print("\n모든 사진 처리 완료!")
        break

    # 다음 배치를 위한 cursor 설정
    if has_more and items:
        cursor = items[-1]['id']

    # 이미 다운로드된 항목 분리
    new_items = [item for item in items if item['id'] not in downloaded_ids]
    skip_count = len(items) - len(new_items)
    total_skipped += skip_count

    print(f"\n===== 배치 {batch} | 전체: {total_count}개 | 신규: {len(new_items)}개 | 기다운로드: {skip_count}개 | 누적: {total_bytes / (1024**3):.2f} GB =====")

    if new_items:
        results = [None] * len(new_items)
        threads = []

        for i, item in enumerate(new_items):
            thread = threading.Thread(target=download_item, args=(item, results, i))
            threads.append(thread)
            thread.start()
            if len(threads) >= THREADS_COUNT:
                for t in threads:
                    t.join()
                threads = []

        for t in threads:
            t.join()

        success = [r for r in results if r and r['success']]
        failed = [r for r in results if r and not r['success']]

        batch_bytes = sum(r['size'] for r in success)
        total_bytes += batch_bytes
        total_downloaded += len(success)
        total_failed += len(failed)

        # CSV에 이력 기록
        save_history(success)

        print(f"\n  다운로드: 성공 {len(success)}개 ({batch_bytes / (1024**2):.1f} MB) / 실패 {len(failed)}개")

        if failed:
            print("  실패 항목:")
            for f in failed:
                print(f"    - {f['id']}: {f['error']}")
    else:
        print("  이번 배치 전부 기다운로드")

    if not has_more:
        print("\n모든 사진 처리 완료!")
        break

print(f"\n===== 최종 결과 =====")
print(f"다운로드 성공: {total_downloaded}개 ({total_bytes / (1024**3):.2f} GB)")
print(f"기다운로드 (다운로드 생략): {total_skipped}개")
print(f"실패: {total_failed}개")
print("(서버에서 삭제하지 않았습니다)")
