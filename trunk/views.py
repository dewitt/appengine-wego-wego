# Copyright 2008 DeWitt Clinton
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import logging

import simplejson

from google.appengine.api import memcache
from google.appengine.api import urlfetch

from django.shortcuts import render_to_response
from django.template.loader import render_to_string
from django import http

from decorator import *

CREF_MIMETYPE = 'text/xml'
ANNOTATIONS_MIMETYPE = 'text/xml'
OSD_MIMETYPE = 'application/opensearchdescription+xml'

CACHE_EXPIRATION = 3600


class ReportableError(Exception):
  """A class of exceptions that should be shown to the user."""
  message = None

  def __init__(self, message):
    """Constructs a new ReportableError.

    Args:
      message: The message to be logged and displayed to the user.
    """
    self.message = message


class UserError(ReportableError):
  """An 400 error caused by user behavior."""


class ServerError(ReportableError):
  """An 500 error caused by the server."""


class RemoteError(ReportableError):
  """An error caused by remote services."""


def cacheable(keygen=None, expiration=CACHE_EXPIRATION):
  """A decorator that caches results in memcache.
  
  keygen: 
    A function that returns the cache key based on the *args and
    **kwargs of the function being called.  If keygen is 
    not specified, the first positional argument will be used.
  expiration:
    The length of time to cache the response in seconds.
  """
  # Define the decorator itself as a closure within cacheable
  def call(f, *args, **kwargs):
    # Don't use the cache at all if there is no expiration
    if not expiration:
      return f(*args, **kwargs)

    # Use the supplied keygen to create a local cache key
    # or use the first positional arg if none is supplied
    if keygen:
      local_key = keygen(*args, **kwargs)
    else:
      local_key = args[0]

    # Create a global cache key that remains stable across instances
    global_key = '%s:%s:%s' % (f.__module__, f.__name__, local_key)

    logging.debug('Checking cache for %s' % local_key)
    result = memcache.get(global_key)
    if result:
      logging.debug('Found %s in cache.' % local_key)
    else:
      logging.debug('Cache miss for %s.' % local_key)
      result = f(*args, **kwargs)
      logging.debug('Caching %s' % local_key)
      if not memcache.add(global_key, result, expiration):
        logging.error('Error caching response for %s.' % local_key)
    return result

  return decorator(call)


def request_keygen(request, *args, **kwargs):
  """Returns a key based on the request path.

  Args:
    request: The http request object.
  """
  if not request or not request.path:
    raise ServerError('First parameter must be a request with a path.')
  return request.path


@cacheable()    
def get_url(url):
  """Retrieves a URL and caches the results.
  
  Args:
    url: A url to be fetched
  Returns:
    a http response
  """
  return urlfetch.fetch(url)


@cacheable()
def get_friendfeed_profile(nickname):
  """Return a friendfeed profile object for a given nickname."""

  if not nickname:
    raise UserError('nickname required')

  friendfeed_profile_url = (
    'http://friendfeed.com/api/user/%s/profile?include=name,nickname,subscriptions' % nickname)

  result = get_url(friendfeed_profile_url)
  if result.status_code == 404:
    raise UserError('User %s not found' % nickname)
  elif result.status_code == 401:
    raise UserError('User %s is private' % nickname)
  elif result.status_code != 200:
    raise ServerError('Unknown friendfeed code' % result.status_code)

  friendfeed_profile_json = result.content

  if not friendfeed_profile_json:
    raise ServerError('could not load friendfeed user %s' % nickname)

  logging.debug('Decoding profile for %s' % nickname)
  friendfeed_profile = simplejson.loads(friendfeed_profile_json)
  if not friendfeed_profile:
    raise ServerError('could not parse friendfeed user %s' % nickname)

  return friendfeed_profile


def _get_friendfeed_name(friendfeed_profile, friendfeed_name):
  """Looks into the profile to get the users real name."""
  try:    
    name = friendfeed_profile['name']
  except KeyError:
    try:
      name = friendfeed_profile['nickname']
    except KeyError:
      name = friendfeed_name    
  return name


def _get_friend_nicknames(friendfeed_profile):
  """Return a list of friend nicknames from the profile."""
  friend_nicknames = []
  for subscription in friendfeed_profile['subscriptions']:
    try:
      friend_nickname = subscription['nickname']
    except:
      logging.warning('No nickname for %s' % subscription)
      continue
    if not friend_nickname:
      logging.warning('No nickname for %s' % subscription)
      continue
    friend_nicknames.append(friend_nickname.lower())
  return friend_nicknames



@cacheable(keygen=request_keygen)
def FaqView(request):
  """Prints the FAQ page."""
  logging.debug('Beginning FaqView handler')
  return render_to_response('faq.tmpl')


@cacheable(keygen=request_keygen)
def HomeView(request):
  """Prints the wego wego homepage"""
  logging.debug('Beginning HomeView handler')
  return render_to_response('home.tmpl')


def UserRedirectView(request):
  """Redirects a form POST to the user view."""
  logging.debug('Beginning handler')
  nickname = request.POST.get('nickname')
  if not nickname:
    raise UserError('nickname required')
  return http.HttpResponseRedirect('/friendfeed/%s/' % nickname)


@cacheable(keygen=request_keygen)
def UserView(request, nickname):
  """A request handler that generates a few demos."""
  logging.debug('Beginning UserView handler')

  if not request.path.islower():
    return http.HttpResponseRedirect(request.path.lower())

  friendfeed_profile = get_friendfeed_profile(nickname)
  name = _get_friendfeed_name(friendfeed_profile, nickname)

  template_data = {'nickname': nickname, 'name':  name}

  return render_to_response('user.tmpl', template_data)


@cacheable(keygen=request_keygen)
def CrefView(request, nickname):
  """A request handler that generates CustomSearch cref files."""
  logging.debug('Beginning CrefView handler')

  if not request.path.islower():
    return http.HttpResponseRedirect(request.path.lower())

  friendfeed_profile = get_friendfeed_profile(nickname)
  name = _get_friendfeed_name(friendfeed_profile, nickname)

  # Google CSE annotation files can only contain 50 at a time
  # so we shard the Include references.  Django templates 
  # don't support math operations, so we pass it a list
  # of start indexes that we precalculate here.
  num_friends = len(_get_friend_nicknames(friendfeed_profile))
  start_indexes = [i * 50 for i in xrange((num_friends / 50) + 1)]

  template_data = {'nickname': nickname, 
                   'name':  name,
                   'start_indexes': start_indexes}

  # Django 0.96's render_to_response does not take a mimetype parameter
  template_string = render_to_string('cref.tmpl', template_data)
  return http.HttpResponse(template_string, mimetype=CREF_MIMETYPE)


@cacheable(keygen=request_keygen)
def AnnotationView(request, nickname, start_index=0):
  """A request handler that generates CustomSearch annotation file."""
  logging.debug('Beginning AnnotationView handler')

  if not request.path.islower():
    return http.HttpResponseRedirect(request.path.lower())

  start_index = int(start_index)
  friendfeed_profile = get_friendfeed_profile(nickname)
  all_friend_nicknames = _get_friend_nicknames(friendfeed_profile)
  end_index = min(len(all_friend_nicknames), start_index + 50)
  friend_nicknames = all_friend_nicknames[start_index:end_index]

  template_data = {'friend_nicknames': friend_nicknames}

  # Django 0.96's render_to_response does not take a mimetype parameter
  template_string = render_to_string('annotations.tmpl', template_data)
  return http.HttpResponse(template_string, mimetype=ANNOTATIONS_MIMETYPE)


@cacheable(keygen=request_keygen)
def OsdView(request, nickname):
  """A request handler that generates an opensearch description document."""
  logging.debug('Beginning OsdView handler')

  if not request.path.islower():
    return http.HttpResponseRedirect(request.path.lower())

  friendfeed_profile = get_friendfeed_profile(nickname)
  name = _get_friendfeed_name(friendfeed_profile, nickname)

  template_data = {'nickname': nickname, 'name':  name}

  # Django 0.96's render_to_response does not take a mimetype parameter
  template_string = render_to_string('osd.tmpl', template_data)
  return http.HttpResponse(template_string, mimetype=OSD_MIMETYPE)

