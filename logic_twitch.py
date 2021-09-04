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
def url_from_id(id):
  if not id.startswith('http'):
    id = 'https://twitch.tv/' + id
  return id



#########################################################
# main logic
#########################################################
class LogicTwitch(LogicModuleBase):
  db_default = {
    'twitch_db_version': '1',
    'twitch_download_path': os.path.join(path_data, P.package_name, 'twitch'),
    'twitch_filename_format': '%Y%m%d.%H%M [{category}] {title}',
    'twitch_directory_name_format': '{author} ({id})/{%Y%m}',
    'twitch_streamer_ids': '',
    'twitch_auto_make_folder': 'True',
    'twitch_auto_start': 'False',
    'twitch_interval': '* * * * *',
    'streamlink_quality': 'best',
    'streamlink_options': '',
  }
  is_running = {}
  children_processes = {}
  streamlink_session = None
  

  def __init__(self, P):
    super(LogicTwitch, self).__init__(P, 'setting', scheduler_desc='twitch 라이브 다운로드')
    self.name = 'twitch'
    default_route_socketio(P, self)
  
  def process_menu(self, sub, req):
    arg = P.ModelSetting.to_dict()
    arg['sub'] = self.name
    if sub in ['setting', 'queue', 'list']:
      if sub == 'setting':
        job_id = f'{self.P.package_name}_{self.name}'
        arg['scheduler'] = str(scheduler.is_include(job_id))
        arg['is_running'] = str(scheduler.is_running(job_id))
        arg['is_streamlink_installed'] = 'Installed' if self.is_streamlink_installed() else 'Not installed'
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
    try:
      if not self.streamlink_session:
        self.streamlink_session = streamlink.Streamlink() 
      
      twitch_prefix = 'https://www.twitch.tv/'
      filename_format = P.ModelSetting.get('twitch_filename_format')
      stream_link_options = P.ModelSetting.get_list('streamlink_options', '|')
      streamer_ids = P.ModelSetting.get_list('twitch_streamer_ids', '|') # | 또는 엔터

      streamlink_plugin_list = {
        streamer_id: self.streamlink_session.resolve_url(twitch_prefix + streamer_id) for streamer_id in streamer_ids
      }
      for streamer_id in streamlink_plugin_list:
        self.is_running[streamer_id] = self.__is_online(streamlink_plugin_list[streamer_id])
        if self.is_running[streamer_id]:
          self.__spawn_children(
            streamer_id, 
            self.__parse_title_string(streamlink_plugin_list[streamer_id], filename_format),
            stream_link_options
          )

      # 방송중이고 현재 다운로드중이 아닌 것
      # 다운로드가 끝나면 is_running을 False로 바꿔야함
    except Exception as e:
      logger.error(f'Exception: {e}')
      logger.error(traceback.format_exc())


  def plugin_load(self):
    streamer_ids = P.ModelSetting.get_list('twitch_streamer_ids', '|') # | 또는 엔터
    for streamer_id in streamer_ids:
      self.is_running[streamer_id] = False
      self.children_processes[streamer_id] = None


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

  # imported from soju6jan/klive/logic_streamlink.py
  @staticmethod
  def is_streamlink_installed():
    try:
      import streamlink
      return True
    except Exception as e: 
      pass
    return False


  def __spawn_children(self, streamer_id, filename, options: list=[]):
    # TODO: https://stackoverflow.com/a/636570
    from subprocess import Popen
    self.children_processes[streamer_id] = Popen(['watch', 'echo', 'test-done'])
    return 'enqueue'

  def __is_online(self, streamlink_plugin):
    return len(streamlink_plugin.streams()) > 0
  
  # TODO: {id} 항목 구현하기
  def __parse_title_string(self, streamlink_plugin, format_str):
    '''
    keywords: {author}, {title}, {category}, {id}
    and time foramt: {%m-%d-%Y %H:%M:%S}
    https://docs.python.org/ko/3/library/datetime.html#strftime-and-strptime-format-codes
    '''
    result = format_str
    result = result.replace('{author}', streamlink_plugin.get_author())
    result = result.replace('{title}', streamlink_plugin.get_title())
    result = result.replace('{category}', streamlink_plugin.get_category())

    idx = 0
    curly_brace_end = 0
    while idx < len(result):
      if result[idx] == '{':
        curly_brace_end = idx
        while curly_brace_end < len(result) and result[curly_brace_end] != '}':
          curly_brace_end += 1
        if result[curly_brace_end] != '}':
          break
        
        time_format = result[idx:curly_brace_end]
        time_string = datetime.datetime.now().strftime(time_format)
        if time_format != time_string:
          result = result[:idx] + time_string + result[curly_brace_end + 1:]
          idx = idx + len(time_string)
      else:
        idx += 1  


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


