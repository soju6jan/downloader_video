# -*- coding: utf-8 -*-
#########################################################
# python
import os, sys, traceback, re, json, threading
from datetime import datetime, timedelta
import copy
# third-party
import requests
# third-party
from flask import request, render_template, jsonify
from sqlalchemy import or_, and_, func, not_, desc
# sjva 공용
from framework import db, scheduler, path_data, socketio
from framework.util import Util
from framework.common.plugin import LogicModuleBase, FfmpegQueueEntity, FfmpegQueue, default_route_socketio
# 패키지
from .plugin import P
logger = P.logger
ModelSetting = P.ModelSetting
#########################################################
headers = {
    'Accept': 'application/json, text/plain, */*',
    'Accept-Language':'ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7',
    'Content-Type':'application/json;charset=UTF-8',
    'Host':'api.aniplustv.com:3100',
    'Origin':'https://www.aniplustv.com',
    'Referer':'https://www.aniplustv.com/',
    'User-Agent':'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/87.0.4280.88 Safari/537.36',
}

class LogicAniplus(LogicModuleBase):
    db_default = {
        'aniplus_db_version' : '1',
        'aniplus_download_path' : os.path.join(path_data, P.package_name, 'aniplus'),
        'aniplus_max_ffmpeg_process_count': '1',
        'aniplus_current_code' : '',
        'aniplus_search_keyword' : '',
        'aniplus_id' : '',
        'aniplus_auto_start' : 'False',
        'aniplus_interval' : '* 5 * * *',
        'aniplus_auto_make_folder' : 'True',
        'aniplus_order_recent' : 'True',
        'aniplus_incompleted_auto_enqueue' : 'True',
        'aniplus_insert_episode_title' : 'False',
        'aniplus_auto_mode_all' : 'False',
        'aniplus_auto_code_list' : 'all',
    }
    
    def __init__(self, P):
        super(LogicAniplus, self).__init__(P, 'setting', scheduler_desc='Aniplus 자동 다운로드')
        self.name = 'aniplus'
        self.queue = FfmpegQueue(P, P.ModelSetting.get_int('aniplus_max_ffmpeg_process_count'))
        self.current_data = None
        self.queue.queue_start()
        default_route_socketio(P, self)

    def process_menu(self, sub, req):
        arg = P.ModelSetting.to_dict()
        arg['sub'] = self.name
        if sub in ['setting', 'queue', 'list', 'request']:
            if sub == 'request' and req.args.get('content_code') is not None:
                arg['aniplus_current_code'] = req.args.get('content_code')
            if sub == 'setting':
                job_id = '%s_%s' % (self.P.package_name, self.name)
                arg['scheduler'] = str(scheduler.is_include(job_id))
                arg['is_running'] = str(scheduler.is_running(job_id))
            return render_template('{package_name}_{module_name}_{sub}.html'.format(package_name=P.package_name, module_name=self.name, sub=sub), arg=arg)
        return render_template('sample.html', title='%s - %s' % (P.package_name, sub))

    def process_ajax(self, sub, req):
        try:
            if sub == 'search':
                keyword = request.form['keyword']
                P.ModelSetting.set('aniplus_search_keyword', keyword)
                data = requests.post('https://api.aniplustv.com:3100/search', headers=headers, data=json.dumps({"params":{"userid":ModelSetting.get('aniplus_id'),"strFind":keyword,"gotoPage":1}})).json()
                return jsonify({'ret':'success', 'data':data})
            elif sub == 'analysis':
                code = request.form['code']
                P.ModelSetting.set('aniplus_current_code', code)
                data = self.get_series_info(code)
                self.current_data = data
                return jsonify({'ret':'success', 'data':data})
            elif sub == 'add_queue':
                ret = {}
                info = json.loads(request.form['data'])
                ret['ret'] = self.add(info)
                return jsonify(ret)
            elif sub == 'entity_list':
                return jsonify(AniplusQueueEntity.get_entity_list())
            elif sub == 'queue_command':
                ret = self.queue.command(req.form['command'], int(req.form['entity_id']))
                return jsonify(ret)
            elif sub == 'add_queue_checked_list':
                data = json.loads(request.form['data'])
                def func():
                    count = 0
                    for tmp in data:
                        add_ret = self.add(tmp)
                        if add_ret.startswith('enqueue'):
                            self.socketio_callback('list_refresh', '')
                            count += 1
                    notify = {'type':'success', 'msg' : u'%s 개의 에피소드를 큐에 추가 하였습니다.' % count}
                    socketio.emit("notify", notify, namespace='/framework', broadcast=True)
                thread = threading.Thread(target=func, args=())
                thread.daemon = True  
                thread.start()
                return jsonify('')
            elif sub == 'web_list':
                return jsonify(ModelAniplusItem.web_list(request))
            elif sub == 'db_remove':
                return jsonify(ModelAniplusItem.delete_by_id(req.form['id']))
        except Exception as e: 
            P.logger.error('Exception:%s', e)
            P.logger.error(traceback.format_exc())

    def setting_save_after(self):
        if self.queue.get_max_ffmpeg_count() != P.ModelSetting.get_int('aniplus_max_ffmpeg_process_count'):
            self.queue.set_max_ffmpeg_count(P.ModelSetting.get_int('aniplus_max_ffmpeg_process_count'))
    
    def scheduler_function(self):
        date = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        data = requests.post('https://api.aniplustv.com:3100/updateWeek', headers=headers, data=json.dumps({"params":{"userid":ModelSetting.get('aniplus_id'),"curDate":date}})).json()[0]['listData']
        data = list(reversed(data))
        conent_code_list = P.ModelSetting.get_list('aniplus_auto_code_list', '|')
        for item in data:
            if date.find(item['startDate']) == -1:
                continue
            if 'all' not in conent_code_list and str(item['contentSerial']) not in conent_code_list:
                continue
            db_entity = ModelAniplusItem.get_by_aniplus_id(item['contentPartSerial'])
            if db_entity is not None:
                continue
            content_info = self.get_series_info(item['contentSerial'])
            if P.ModelSetting.get_bool('aniplus_auto_mode_all'):
                for episode_info in content_info['episode']:
                    add_ret = self.add(episode_info)
                    if add_ret.startswith('enqueue'):
                        self.socketio_callback('list_refresh', '')
            else:
                episode_info = content_info['episode'][0] if content_info['recent'] else content_info['episode'][-1]
                add_ret = self.add(episode_info)
                if add_ret.startswith('enqueue'):
                    self.socketio_callback('list_refresh', '')

    def plugin_load(self):
        if P.ModelSetting.get_bool('aniplus_incompleted_auto_enqueue'):
            def func():
                data = ModelAniplusItem.get_list_incompleted()
                for db_entity in data:
                    add_ret = self.add(db_entity.aniplus_info)
                    if add_ret.startswith('enqueue'):
                        self.socketio_callback('list_refresh', '')
            thread = threading.Thread(target=func, args=())
            thread.daemon = True  
            thread.start()

    def reset_db(self):
        db.session.query(ModelAniplusItem).delete()
        db.session.commit()
        return True

    #########################################################
    def add(self, episode_info):
        if AniplusQueueEntity.is_exist(episode_info):
            return 'queue_exist'
        else:
            db_entity = ModelAniplusItem.get_by_aniplus_id(episode_info['contentPartSerial'])
            if db_entity is None:
                entity = AniplusQueueEntity(P, self, episode_info)
                if entity.available:
                    ModelAniplusItem.append(entity.as_dict())
                    self.queue.add_queue(entity)
                    return 'enqueue_db_append'
                else:
                    return 'fail'
            elif db_entity.status != 'completed':
                entity = AniplusQueueEntity(P, self, episode_info)
                self.queue.add_queue(entity)
                return 'enqueue_db_exist'
            else:
                return 'db_completed'

    def get_series_info(self, code):
        try:
            if self.current_data is not None and 'code' in self.current_data and self.current_data['code'] == code and self.current_data['recent'] == ModelSetting.get_bool('aniplus_order_recent'):
                return self.current_data
            data = {}
            data['code'] = code
            tmp = requests.get('https://api.aniplustv.com:3100/itemInfo?contentSerial={code}'.format(code=code), headers=headers).json()
            if tmp[0]['intReturn'] == 0:
                data['info'] = tmp[0]['listData'][0]
            tmp = requests.get('https://api.aniplustv.com:3100/itemPart?contentSerial={code}&userid={id}'.format(code=code, id=ModelSetting.get('aniplus_id')), headers=headers).json()
            if tmp[0]['intReturn'] == 0:
                data['episode'] = tmp[0]['listData']
                logger.debug(P.ModelSetting.get_bool('aniplus_order_desc'))
                if P.ModelSetting.get_bool('aniplus_order_recent') == False:
                    data['episode'] = list(reversed(data['episode']))
            data['recent'] = ModelSetting.get_bool('aniplus_order_recent')
            return data
        except Exception as e:
            P.logger.error('Exception:%s', e)
            P.logger.error(traceback.format_exc())
            return {'ret':'exception', 'log':str(e)}




class AniplusQueueEntity(FfmpegQueueEntity):
    def __init__(self, P, module_logic, info):
        super(AniplusQueueEntity, self).__init__(P, module_logic, info)
        self.season = 1
        self.available = self.make_episode_info()
        
    def refresh_status(self):
        self.module_logic.socketio_callback('status', self.as_dict())

    def info_dict(self, tmp):
        for key, value in self.info.items():
            tmp[key] = value
        tmp['season'] = self.season
        tmp['aniplus_info'] = self.info
        return tmp

    def donwload_completed(self):
        db_entity = ModelAniplusItem.get_by_aniplus_id(self.info['contentPartSerial'])
        if db_entity is not None:
            db_entity.status = 'completed'
            db_entity.complated_time = datetime.now()
            db_entity.save()

    def make_episode_info(self):
        try:
            data = requests.get('https://www.aniplustv.com/aniplus2020Api/base64encode.asp?codeString={time}|aniplus|{subPartSerial2}/{userid}'.format(
                time=(datetime.now() + timedelta(hours=1)).strftime('%Y%m%d%H%M%S'),
                subPartSerial2=self.info['subPartSerial2'],
                userid=ModelSetting.get('aniplus_id'),
            ), headers=headers).text
            data = data.replace('{"codeString":"', '').replace('"}', '')
            data = requests.post('https://api.aniplustv.com:3100/vodUrl', headers=headers, data=json.dumps({"params":{"userid":ModelSetting.get('aniplus_id'),"subPartSerial":self.info['subPartSerial2'],"crypParam":data}})).json()[0]
            if 'use1080' in data and data['use1080'] != '':
                self.quality = '1080p'
                self.url = data['use1080']
            elif 'fileName1080p' in data and data['fileName1080p'] != '':
                self.quality = '1080p'
                self.url = data['fileName1080p']
            elif 'fileName720p' in data and data['fileName720p'] != '':
                self.quality = '720p'
                self.url = data['fileName720p']
            elif 'fileName480p' in data and data['fileName480p'] != '':
                self.quality = '480p'
                self.url = data['fileName480p']
            else:
                return False
            match = re.compile(r'(?P<title>.*?)\s*((?P<season>\d+)%s)' % (u'기')).search(self.info['title'])
            if match:
                content_title = match.group('title').strip()
                if 'season' in match.groupdict() and match.group('season') is not None:
                    self.season = int(match.group('season'))
            else:
                content_title = self.info['title']
                self.season = 1
            if content_title.find(u'극장판') != -1:
                ret = '%s.%s-SA+.mp4' % (content_title, self.quality)
            else:
                ret = '%s.S%sE%s.%s-SA+.mp4' % (content_title, str(self.season).zfill(2), str(self.info['part']).zfill(2), self.quality)
            self.filename = Util.change_text_for_use_filename(ret)
            self.savepath = P.ModelSetting.get('aniplus_download_path')
            if P.ModelSetting.get_bool('aniplus_auto_make_folder'):
                folder_name = Util.change_text_for_use_filename ( content_title.strip() )
                self.savepath = os.path.join(self.savepath, folder_name)
            self.filepath = os.path.join(self.savepath, self.filename)
            if not os.path.exists(self.savepath):
                os.makedirs(self.savepath)
            return True
        except Exception as e:
            P.logger.error('Exception:%s', e)
            P.logger.error(traceback.format_exc())

    @classmethod
    def is_exist(cls, info):
        for e in cls.entity_list:
            if e.info['contentPartSerial'] == info['contentPartSerial']:
                return True
        return False


class ModelAniplusItem(db.Model):
    __tablename__ = '{package_name}_aniplus_item'.format(package_name=P.package_name)
    __table_args__ = {'mysql_collate': 'utf8_general_ci'}
    __bind_key__ = P.package_name
    id = db.Column(db.Integer, primary_key=True)
    created_time = db.Column(db.DateTime)
    completed_time = db.Column(db.DateTime)
    reserved = db.Column(db.JSON)
    content_code = db.Column(db.String)
    season = db.Column(db.Integer)
    episode_no = db.Column(db.Integer)
    title = db.Column(db.String)
    episode_title = db.Column(db.String)
    aniplus_id = db.Column(db.String)
    quality = db.Column(db.String)
    filepath = db.Column(db.String)
    filename = db.Column(db.String)
    savepath = db.Column(db.String)
    video_url = db.Column(db.String)
    thumbnail = db.Column(db.String)
    status = db.Column(db.String)
    aniplus_info = db.Column(db.JSON)

    def __init__(self):
        self.created_time = datetime.now()

    def __repr__(self):
        return repr(self.as_dict())

    def as_dict(self):
        ret = {x.name: getattr(self, x.name) for x in self.__table__.columns}
        ret['created_time'] = self.created_time.strftime('%Y-%m-%d %H:%M:%S') 
        ret['completed_time'] = self.completed_time.strftime('%Y-%m-%d %H:%M:%S') if self.completed_time is not None else None
        return ret

    def save(self):
        db.session.add(self)
        db.session.commit()

    @classmethod
    def get_by_id(cls, id):
        return db.session.query(cls).filter_by(id=id).first()
    
    @classmethod
    def get_by_aniplus_id(cls, aniplus_id):
        return db.session.query(cls).filter_by(aniplus_id=aniplus_id).first()

    @classmethod
    def delete_by_id(cls, id):
        db.session.query(cls).filter_by(id=id).delete()
        db.session.commit()
        return True

    @classmethod
    def web_list(cls, req):
        ret = {}
        page = int(req.form['page']) if 'page' in req.form else 1
        page_size = 30
        job_id = ''
        search = req.form['search_word'] if 'search_word' in req.form else ''
        option = req.form['option'] if 'option' in req.form else 'all'
        order = req.form['order'] if 'order' in req.form else 'desc'
        query = cls.make_query(search=search, order=order, option=option)
        count = query.count()
        query = query.limit(page_size).offset((page-1)*page_size)
        lists = query.all()
        ret['list'] = [item.as_dict() for item in lists]
        ret['paging'] = Util.get_paging_info(count, page, page_size)
        return ret

    @classmethod
    def make_query(cls, search='', order='desc', option='all'):
        query = db.session.query(cls)
        if search is not None and search != '':
            if search.find('|') != -1:
                tmp = search.split('|')
                conditions = []
                for tt in tmp:
                    if tt != '':
                        conditions.append(cls.filename.like('%'+tt.strip()+'%') )
                query = query.filter(or_(*conditions))
            elif search.find(',') != -1:
                tmp = search.split(',')
                for tt in tmp:
                    if tt != '':
                        query = query.filter(cls.filename.like('%'+tt.strip()+'%'))
            else:
                query = query.filter(cls.filename.like('%'+search+'%'))
        if option == 'completed':
            query = query.filter(cls.status == 'completed')

        query = query.order_by(desc(cls.id)) if order == 'desc' else query.order_by(cls.id)
        return query  

    @classmethod
    def get_list_incompleted(cls):
        return db.session.query(cls).filter(cls.status != 'completed').all()

    @classmethod
    def append(cls, q):
        item = ModelAniplusItem()
        item.content_code = q['contentSerial']
        item.aniplus_id = q['contentPartSerial']
        item.episode_no = q['part']
        item.title = q['title']
        item.episode_title = q['subTitle']
        item.season = q['season']
        item.quality = q['quality']
        item.filepath = q['filepath']
        item.filename = q['filename']
        item.savepath = q['savepath']
        item.video_url = q['url']
        item.thumbnail = q['img']
        item.status = 'wait'
        item.aniplus_info = q['aniplus_info']
        item.save()


