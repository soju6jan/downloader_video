# -*- coding: utf-8 -*-
#########################################################
# imports
#########################################################
# python
import os, sys, traceback, re, json, threading, base64
from datetime import datetime, timedelta
import copy
# third-party
import requests
# third-party
from flask import request, render_template, jsonify
from sqlalchemy import or_, and_, func, not_, desc
# sjva 공용
from framework import app, db, scheduler, path_data, socketio
from framework.util import Util
from plugin import LogicModuleBase, default_route_socketio
# 패키지
from .plugin import P
logger = P.logger
ModelSetting = P.ModelSetting

#########################################################
# utils
#########################################################


#########################################################
# main logic
#########################################################
class LogicTwitch(LogicModuleBase):
  db_default = {
    'twitch_db_version': '1',
    'twitch_download_path': os.path.join(path_data, P.package_name, 'twitch'),
    'twitch_filename_format': '[%Y%m%d][%H%M] [{category}] {title}',
    'twitch_directory_name_format': '{author} ({id})/%Y-%m',
    'twitch_streamer_ids': '',
    'twitch_auto_make_folder': 'True',
    'twitch_auto_start': 'False',
    'twitch_interval': '* * * * *',
    'streamlink_quality': 'best',
    'streamlink_options': '--force-progress\n--twitch-disable-hosting\n--twitch-disable-ads\n--twitch-disable-reruns\n',
  }
  twitch_prefix = 'https://www.twitch.tv/'
  is_streamlink_installed = False
  streamlink_plugins = {}
  '''
  'streamer_id': <Streamlink.plugin>
  '''
  streamlink_infos = {} 
  '''
  'streamer_id': {
    online: bool,
    author: str,
    title: str,
    category: str,
  } 
  '''
  children_processes = {}
  '''
  'streamer_id': <subprocess.Popen> 
  '''
  streamlink_session = None
  

  def __init__(self, P):
    super(LogicTwitch, self).__init__(P, 'setting', scheduler_desc='twitch 라이브 다운로드')
    self.name = 'twitch'
    default_route_socketio(P, self)
  
  def process_menu(self, sub, req):
    arg = P.ModelSetting.to_dict()
    arg['sub'] = self.name
    if sub in ['setting', 'status', 'list']:
      if sub == 'setting':
        job_id = f'{self.P.package_name}_{self.name}'
        arg['scheduler'] = str(scheduler.is_include(job_id))
        arg['is_running'] = str(scheduler.is_running(job_id))
        arg['is_streamlink_installed'] = 'Installed' if self.is_streamlink_installed else 'Not installed'
      return render_template(f'{P.package_name}_{self.name}_{sub}.html', arg=arg)
    return render_template('sample.html', title=f'404: {P.package_name} - {sub}')
  

  def process_ajax(self, sub, req):
    try:
      if sub == 'entity_list': # 처음에 queue에서 요청. 로컬에서 항목 추가되면 list_refresh 소켓 보내기
        return jsonify({})
      elif sub == 'web_list':
        return jsonify({})
      elif sub == 'install':
        LogicTwitch.install_streamlink()
        try:
          import streamlink
          self.is_streamlink_installed = True
        except:
          pass
        return jsonify({})
      elif sub == 'db_remove':
        return jsonify(ModelTwitchItem.delete_by_id(req.form['id']))
    except Exception as e:
      logger.error(f'Exception: {e}')
      logger.error(traceback.format_exc())


  def setting_save_after(self):
    pass
    # TODO: streamlink_session에 옵션 세팅하기


  def scheduler_function(self):
    # TODO: 이미 실행중이면 스킵하기
    try:
      if not self.streamlink_session:
        import streamlink
        self.streamlink_session = streamlink.Streamlink() 
      
      streamlink_quality = P.ModelSetting.get('streamlink_quality')
      streamlink_options = P.ModelSetting.get_list('streamlink_options', '|')
      streamer_ids = P.ModelSetting.get_list('twitch_streamer_ids', '|') # | 또는 엔터

      download_directory = P.ModelSetting.get('twitch_download_path')
      make_child_directory = P.ModelSetting.get_bool('twitch_auto_make_folder')
      child_directory_name_format = P.ModelSetting.get('twitch_directory_name_format')
      filename_format = P.ModelSetting.get('twitch_filename_format')

      self.streamlink_plugins = {
        streamer_id: self.streamlink_session.resolve_url(self.twitch_prefix + streamer_id) for streamer_id in streamer_ids
      }
      for streamer_id in self.streamlink_plugins:
        self.__set_streamlink_info(streamer_id)
        if self.streamlink_infos[streamer_id]['online']:
          # set filename
          download_path = os.path.abspath(download_directory)
          if make_child_directory:
            directory_name = self.__parse_title_string(streamer_id, child_directory_name_format)
            download_path = os.path.join(download_path, directory_name)
            if not os.path.isdir(download_path):
              os.makedirs(download_path, exist_ok=True) # mkdir -p
          filename = self.__parse_title_string(streamer_id, filename_format)
          download_path = os.path.join(download_path, filename)
          if not download_path.endswith('.mp4'):
            download_path = download_path + '.mp4'
          # spawn child
          self.__spawn_children(
            streamer_id, 
            download_path,
            quality=streamlink_quality,
            options=streamlink_options
          )
    except Exception as e:
      logger.error(f'Exception: {e}')
      logger.error(traceback.format_exc())


  def plugin_load(self):
    try:
      import streamlink
      self.is_streamlink_installed = True
    except:
      return False
  

  def reset_db(self):
    db.session.query(ModelTwitchItem).delete()
    db.session.commit()
    return True

  #########################################################

  # imported from soju6jan/klive/logic_streamlink.py
  @staticmethod
  def install_streamlink():
    try:
      def func():
        import system
        import framework.common.util as CommonUtil
        commands = [['msg', u'잠시만 기다려주세요.']]
        if CommonUtil.is_docker():
          commands.append(['apk', 'add', '--no-cache', '--virtual', '.build-deps', 'gcc', 'g++', 'make', 'libffi-dev', 'openssl-dev'])
        
        commands.append([app.config['config']['pip'], 'install', '--upgrade', 'pip'])
        commands.append([app.config['config']['pip'], 'install', '--upgrade', 'setuptools'])
        commands.append([app.config['config']['pip'], 'install', 'streamlink'])
        if CommonUtil.is_docker():
          commands.append(['apk', 'del', '.build-deps'])
        commands.append(['msg', u'설치가 완료되었습니다.'])
        system.SystemLogicCommand.start('설치', commands)
      t = threading.Thread(target=func, args=())
      t.setDaemon(True)
      t.start()
    except Exception as e: 
      logger.error('Exception:%s', e)
      logger.error(traceback.format_exc())

  """
  [cli][info] streamlink is running as root! Be careful!
  [cli][info] Found matching plugin twitch for URL https://www.twitch.tv/jungtaejune
  [cli][info] Available streams: audio_only, 160p (worst), 360p, 480p, 720p, 720p60, 1080p60 (best)
  [cli][info] Opening stream: 1080p60 (hls)
  [plugins.twitch][info] Will skip ad segments
  [download][ddol_210905_1530.mp4] Written 23.68 GB (6h43m50s @ 1005.8 KB/s)  
  """
  def __spawn_children(self, streamer_id, filename, quality='best', options: list=[]):
    try:
      def output_reader(proc, streamer_id):
        for line in iter(proc.stdout.readline, b''):
          print(f'[{streamer_id}]{line}', end='')
          # print(f'[{streamer_id}]{line.decode("utf-8")}', end='')
      # TODO: https://stackoverflow.com/a/636570
      # 지금 output_reader에서는 현재 진행상황이 표시가 안되네 
      # progress 표시하는게 버퍼에 쌓이지 않아서 그럼
      # -> bufsize=0, universal_newlines=True으로 해결
      command = ['python3', '-m', 'streamlink', '--output', filename]
      command = command + options
      command = command + [f'{self.twitch_prefix}{streamer_id}', quality]

      from subprocess import Popen, PIPE, STDOUT
      self.children_processes[streamer_id] = Popen(
        command, 
        stdout=PIPE, 
        stderr=STDOUT,
        universal_newlines=True,
        bufsize=0,
      )
      t = threading.Thread(target=output_reader, args=(self.children_processes[streamer_id], streamer_id,))
      t.setDaemon(True)
      t.start()
      return f'spwan success: {streamer_id}'
    except Exception as e:
      logger.error(f'Exception: {e}')
      logger.error(traceback.format_exc())
      return f'spawn failed: {streamer_id}'


  def __set_streamlink_info(self, streamer_id):
    info = {}
    plugin = self.streamlink_plugins[streamer_id]
    # TODO: 특수문자 이스케이프
    author = plugin.get_author() or 'No Author'
    title = plugin.get_title()
    category = plugin.get_category() or 'No Category'

    # 웹에서 띄어쓰기가 없거나 하나인데 여러개로 표시되는 문제
    title = title.strip() if type(title) == type('') else 'No Title'
    title = re.sub(r'(\s)+', r'\1', title)

    # 파일명에 불가능한 문자. :, \, /, : * ? " < > |
    replace_list = {
      ':': '∶',
      '/': '_',
      '\\': '_',
      '*': '⁎',
      '?': '？',
      '"': "'",
      '<': '(',
      '>': ')',
      '|': '_',
    }
    for key in replace_list.keys():
      author = author.replace(key, replace_list[key])
      title = title.replace(key, replace_list[key])
      category = category.replace(key, replace_list[key])
    
    info['online'] = len(plugin.streams()) > 0
    info['author'] = author
    info['title'] = title
    info['category'] = category
    self.streamlink_infos[streamer_id] = info
  

  def __parse_title_string(self, streamer_id, format_str):
    '''
    keywords: {author}, {title}, {category}, {id}
    and time foramt keywords: %m,%d,%Y, %H,%M,%S, ...
    https://docs.python.org/ko/3/library/datetime.html#strftime-and-strptime-format-codes
    '''
    result = format_str
    result = result.replace('{id}', streamer_id)
    result = result.replace('{author}', self.streamlink_infos[streamer_id]['author'])
    result = result.replace('{title}', self.streamlink_infos[streamer_id]['title'])
    result = result.replace('{category}', self.streamlink_infos[streamer_id]['category'])
    result = datetime.now().strftime(result)
    return result


#########################################################
# db 
#########################################################
class ModelTwitchItem(db.Model):
  __tablename__ = '{package_name}_twitch_item'.format(package_name=P.package_name)
  __table_args__ = {'mysql_collate': 'utf8_general_ci'}
  __bind_key__ = P.package_name
  id = db.Column(db.Integer, primary_key=True)
  created_time = db.Column(db.DateTime)
  completed_time = db.Column(db.DateTime)
  streamer_name = db.Column(db.String)
  streamer_id = db.Column(db.String)
  title = db.Column(db.String)
  quality = db.Column(db.String)
  filepath = db.Column(db.String)
  filename = db.Column(db.String)
  savepath = db.Column(db.String)
  status = db.Column(db.String)

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


