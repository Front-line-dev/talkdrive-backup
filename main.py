import os
import requests
import json
import time
import shutil
import threading

BACKUP_PATH = './backups'
REQ_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36',
    'Accept': 'application/json+javascript'
}

if not os.path.exists(BACKUP_PATH):
    os.makedirs(BACKUP_PATH)

# 쿠키 정보 읽기
cookies = {}

def is_kakao_cookie(line):
    if line.startswith('drive.kakao.com') or line.startswith('.kakao.com'):
        return True
    else:
        return False

with open('drive.kakao.com_cookies.txt', 'r', encoding='utf-8') as file:
    lines = file.readlines()
    cookie_lines = [line for line in lines if is_kakao_cookie(line)]
    for cookie_line in cookie_lines:
        name, value = cookie_line.split('\t')[5:7]
        cookies[name.replace(' ', '')] = value.replace('\n', '')

# 요청 및 다운로드
def request_list(url):
    try:
        response = requests.get(url, cookies=cookies, headers=REQ_HEADERS)
        response_content = response.content.decode('utf-8')
        response_json = json.loads(response_content)
        return response_json
    except Exception as e:
        print('error on request get list', url)
        print(e)

def request_photo(url):
    try:
        response = requests.get(f'{url}?attach', cookies=cookies, headers=REQ_HEADERS)
        return response.content
    except Exception as e:
        print('error on request get photo', url)
        print(e)

def request_delete(file_list_json):
    data = {
        "verticalType": "MEDIA",
        "ids": [photo_item['id'] for photo_item in file_list_json['items']]
    }

    try:
        url = 'https://drawer-api.kakao.com/mediaFile/delete'
        response = requests.post(url, data=json.dumps(data), headers={'Content-Type': 'application/json', 'Accept': 'application/json+javascript'}, cookies=cookies)
        # 204 성공
        return response
    except Exception as e:
        print('error on request post kakao')
        print(e)


while(True):
    ## 파일 리스트 요청
    # https://drawer-api.kakao.com/mediaFile/list?verticalType=MEDIA&fetchCount=100&joined=true&direction=DESC
    # https://drawer-api.kakao.com/mediaFile/list?verticalType=MEDIA&fetchCount=100&joined=true&direction=ASC
    # https://drawer-api.kakao.com/mediaFile/list?verticalType=MEDIA&fetchCount=100&joined=true&direction=ASC
    file_list_json = request_list('https://drawer-api.kakao.com/mediaFile/list?verticalType=MEDIA&fetchCount=100&joined=true&direction=ASC')

    ## 파일이 없는지(끝났는지) 검사
    if file_list_json['totalCount'] == 0:
        break

    ## 다운로드할 파일을 위한 폴더 생성
    timestamp = int(time.time())
    download_path = f'{BACKUP_PATH}/{timestamp}'
    os.makedirs(download_path)

    ## 요청 metadata 저장
    with open(f'{BACKUP_PATH}/{timestamp}_list.json', 'w') as file:
        json.dump(file_list_json, file)

    ## 사진 저장
    PHOTO_COUNT = 100
    THREADS_COUNT = 5
    def worker(photo_item_list):
        for photo_item in photo_item_list:
            photo = request_photo(photo_item['url'])
            print('download', photo_item['id'])
            with open(f"{download_path}/{photo_item['url'].split('/')[-1]}", 'wb') as f:
                f.write(photo)

    photo_item_list_list = [list() for _ in range(THREADS_COUNT)]

    for index, photo_item in enumerate(file_list_json['items']):
        photo_item_list_list[index // (PHOTO_COUNT // THREADS_COUNT)].append(photo_item)

    threads = []

    for i in range(THREADS_COUNT):
        thread = threading.Thread(target=worker, args=(photo_item_list_list[i],))
        threads.append(thread)
        thread.start()

    for thread in threads:
        thread.join()

    ## 압축
    shutil.make_archive(f'{download_path}_photo', 'zip', download_path)

    ## 폴더 삭제
    shutil.rmtree(download_path)

    ## 클라우드 사진 삭제
    request_delete(file_list_json)
    print('deleted downloaded photo')
