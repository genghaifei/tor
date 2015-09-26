#!/usr/bin/python

# Usage: scripts/maint/updateFallbackDirs.py > src/or/fallback_dirs.inc
# Then read the generated list of string to ensure no-one slipped
# anything funny into it

# Script by weasel, April 2015
# Portions by gsathya & karsten, 2013
# https://trac.torproject.org/projects/tor/attachment/ticket/8374/dir_list.2.py
# Modifications by teor, May-August 2015

import StringIO
import string
import re
import datetime
import gzip
import os.path
import json
import math
import sys
import urllib
import urllib2
import hashlib
# bson_lazy provides bson
#from bson import json_util

import logging
logging.basicConfig(level=logging.INFO)

ONIONOO = 'https://onionoo.torproject.org/'

# Don't bother going out to the Internet, just use the files available locally,
# even if they're very old
LOCAL_FILES_ONLY = False

ADDRESS_AND_PORT_STABLE_DAYS = 120
# What time-weighted-fraction of these flags must FallbackDirs
# Equal or Exceed?
CUTOFF_RUNNING = .95
CUTOFF_V2DIR = .95
CUTOFF_GUARD = .95
# What time-weighted-fraction of these flags must FallbackDirs
# Equal or Fall Under?
# .00 means no bad exits
PERMITTED_BADEXIT = .00

# Limit the number of fallbacks (eliminating lowest by weight)
MAX_FALLBACK_COUNT = 500
# Emit a C #error if the number of fallbacks is below
MIN_FALLBACK_COUNT = 100

# Limit the proportional weight
# If a single fallback's weight is too high, it will see too many clients
# We reweight using a lower threshold to provide some leeway for:
# * elimination of low weight relays
# * consensus weight changes
# * fallback directory losses over time
# With 2 million users per day, a relay weighted at 1 in 50 fallbacks will
# receive 40,000 requests per day, or about 1 request every two seconds.
# It will see about 2% of clients.
TARGET_MAX_WEIGHT_FRACTION = 1/50.0
REWEIGHTING_FUDGE_FACTOR = 0.8
MAX_WEIGHT_FRACTION = TARGET_MAX_WEIGHT_FRACTION * REWEIGHTING_FUDGE_FACTOR
# If a single fallback's weight is too low, it's pointless adding it.
# (Final weights may be slightly higher than this, due to low weight relays
# being excluded.)
# With 2 million users per day, a relay weighted at 1 in 2000 fallbacks will
# receive 1000 requests per day, or about 1 request every minute or two.
# It will see less than 0.1% of clients.
MIN_WEIGHT_FRACTION = 1/2000.0

AGE_ALPHA = 0.99 # older entries' weights are adjusted with ALPHA^(age in days)

ONIONOO_SCALE_ONE = 999.


def parse_ts(t):
  return datetime.datetime.strptime(t, "%Y-%m-%d %H:%M:%S")

def remove_bad_chars(raw_string, bad_char_list):
  # Remove each character in the bad_char_list
  escaped_string = raw_string
  for c in bad_char_list:
    escaped_string = escaped_string.replace(c, '')
  return escaped_string

def cleanse_whitespace(raw_string):
  # Replace all whitespace characters with a space
  escaped_string = raw_string
  for c in string.whitespace:
    escaped_string = escaped_string.replace(c, ' ')
  return escaped_string

def cleanse_c_multiline_comment(raw_string):
  # Prevent a malicious / unanticipated string from breaking out
  # of a C-style multiline comment
  # This removes '/*' and '*/'
  # To deal with '//', the end comment must be on its own line
  bad_char_list = '*'
  # Prevent a malicious string from using C nulls
  bad_char_list += '\0'
  # Be safer by removing bad characters entirely
  escaped_string = remove_bad_chars(raw_string, bad_char_list)
  # Embedded newlines should be removed by tor/onionoo, but let's be paranoid
  escaped_string = cleanse_whitespace(escaped_string)
  # Some compilers may further process the content of comments
  # There isn't much we can do to cover every possible case
  # But comment-based directives are typically only advisory
  return escaped_string

def cleanse_c_string(raw_string):
  # Prevent a malicious address/fingerprint string from breaking out
  # of a C-style string
  bad_char_list = '"'
  # Prevent a malicious string from using escapes
  bad_char_list += '\\'
  # Prevent a malicious string from using C nulls
  bad_char_list += '\0'
  # Be safer by removing bad characters entirely
  escaped_string = remove_bad_chars(raw_string, bad_char_list)
  # Embedded newlines should be removed by tor/onionoo, but let's be paranoid
  escaped_string = cleanse_whitespace(escaped_string)
  # Some compilers may further process the content of strings
  # There isn't much we can do to cover every possible case
  # But this typically only results in changes to the string data
  return escaped_string

# a dictionary of source metadata for each onionoo query we've made
fetch_source = {}

# register source metadata for 'what'
# assumes we only retrieve one document for each 'what'
def register_fetch_source(what, url, relays_published, version):
  fetch_source[what] = {}
  fetch_source[what]['url'] = url
  fetch_source[what]['relays_published'] = relays_published
  fetch_source[what]['version'] = version

# list each registered source's 'what'
def fetch_source_list():
  return sorted(fetch_source.keys())

# given 'what', provide a multiline C comment describing the source
def describe_fetch_source(what):
  desc = '/*'
  desc += '\n'
  desc += 'Onionoo Source: '
  desc += cleanse_c_multiline_comment(what)
  desc += ' Date: '
  desc += cleanse_c_multiline_comment(fetch_source[what]['relays_published'])
  desc += ' Version: '
  desc += cleanse_c_multiline_comment(fetch_source[what]['version'])
  desc += '\n'
  desc += 'URL: '
  desc += cleanse_c_multiline_comment(fetch_source[what]['url'])
  desc += '\n'
  desc += '*/'
  return desc

def write_to_file(str, file_name, max_len):
  try:
    with open(file_name, 'w') as f:
      f.write(str[0:max_len])
  except EnvironmentError, error:
    logging.debug('Writing file %s failed: %d: %s'%
                  (file_name,
                   error.errno,
                   error.strerror)
                  )

def read_from_file(file_name, max_len):
  try:
    if os.path.isfile(file_name):
      with open(file_name, 'r') as f:
        return f.read(max_len)
  except EnvironmentError, error:
    logging.debug('Loading file %s failed: %d: %s'%
                  (file_name,
                   error.errno,
                   error.strerror)
                  )
  return None

def load_possibly_compressed_response_json(response):
    if response.info().get('Content-Encoding') == 'gzip':
      buf = StringIO.StringIO( response.read() )
      f = gzip.GzipFile(fileobj=buf)
      return json.load(f)
    else:
      return json.load(response)

def load_json_from_file(json_file_name):
    # An exception here may be resolved by deleting the .last_modified
    # and .json files, and re-running the script
    try:
      with open(json_file_name, 'r') as f:
        return json.load(f)
    except EnvironmentError, error:
      raise Exception('Reading not-modified json file %s failed: %d: %s'%
                    (json_file_name,
                     error.errno,
                     error.strerror)
                    )

def onionoo_fetch(what, **kwargs):
  params = kwargs
  params['type'] = 'relay'
  #params['limit'] = 10
  params['first_seen_days'] = '%d-'%(ADDRESS_AND_PORT_STABLE_DAYS,)
  params['last_seen_days'] = '-7'
  params['flag'] = 'V2Dir'
  url = ONIONOO + what + '?' + urllib.urlencode(params)

  # Unfortunately, the URL is too long for some OS filenames,
  # but we still don't want to get files from different URLs mixed up
  base_file_name = what + '-' + hashlib.sha1(url).hexdigest()

  full_url_file_name = base_file_name + '.full_url'
  MAX_FULL_URL_LENGTH = 1024

  last_modified_file_name = base_file_name + '.last_modified'
  MAX_LAST_MODIFIED_LENGTH = 64

  json_file_name = base_file_name + '.json'

  if LOCAL_FILES_ONLY:
    # Read from the local file, don't write to anything
    response_json = load_json_from_file(json_file_name)
  else:
    # store the full URL to a file for debugging
    # no need to compare as long as you trust SHA-1
    write_to_file(url, full_url_file_name, MAX_FULL_URL_LENGTH)

    request = urllib2.Request(url)
    request.add_header('Accept-encoding', 'gzip')

    # load the last modified date from the file, if it exists
    last_mod_date = read_from_file(last_modified_file_name,
                                   MAX_LAST_MODIFIED_LENGTH)
    if last_mod_date is not None:
      request.add_header('If-modified-since', last_mod_date)

    response_code = 0
    try:
      response = urllib2.urlopen(request)
      response_code = response.getcode()
    except urllib2.HTTPError, error:
      response_code = error.code
      if response_code == 304: # Not Modified
        pass
      else:
        raise Exception("Could not get " + url + ": "
                        + str(error.code) + ": " + error.reason)

    if response_code == 200: # OK

      response_json = load_possibly_compressed_response_json(response)

      with open(json_file_name, 'w') as f:
        # use the most compact json representation to save space
        json.dump(response_json, f, separators=(',',':'))

      # store the last modified date in its own file
      if response.info().get('Last-modified') is not None:
        write_to_file(response.info().get('Last-Modified'),
                      last_modified_file_name,
                      MAX_LAST_MODIFIED_LENGTH)

    elif response_code == 304: # Not Modified

      response_json = load_json_from_file(json_file_name)

    else: # Unexpected HTTP response code not covered in the HTTPError above
      raise Exception("Unexpected HTTP response code to " + url + ": "
                      + str(response_code))

  register_fetch_source(what,
                        url,
                        response_json['relays_published'],
                        response_json['version'])

  return response_json

def dummy_fetch(what, **kwargs):
  with open('x-'+what) as f:
    return json.load(f)

def fetch(what, **kwargs):
  #x = onionoo_fetch(what, **kwargs)
  # don't use sort_keys, as the order of or_addresses is significant
  #print json.dumps(x, indent=4, separators=(',', ': '))
  #sys.exit(0)

  return onionoo_fetch(what, **kwargs)
  #return dummy_fetch(what, **kwargs)


class Candidate(object):
  CUTOFF_ADDRESS_AND_PORT_STABLE = (datetime.datetime.now()
                            - datetime.timedelta(ADDRESS_AND_PORT_STABLE_DAYS))

  def __init__(self, details):
    for f in ['fingerprint', 'nickname', 'last_changed_address_or_port',
              'consensus_weight', 'or_addresses', 'dir_address']:
      if not f in details: raise Exception("Document has no %s field."%(f,))

    if not 'contact' in details: details['contact'] = None
    details['last_changed_address_or_port'] = parse_ts(
                                      details['last_changed_address_or_port'])

    self._data = details
    self._stable_sort_or_addresses()

    self._fpr = self._data['fingerprint']
    self._running = self._guard = self._v2dir = 0.
    self._compute_orport()
    if self.orport is None:
      raise Exception("Failed to get an orport for %s."%(self._fpr,))
    self._compute_ipv6addr()
    if self.ipv6addr is None:
      logging.debug("Failed to get an ipv6 address for %s."%(self._fpr,))

  def _stable_sort_or_addresses(self):
    # replace self._data['or_addresses'] with a stable ordering,
    # sorting the secondary addresses in string order
    # leave the received order in self._data['or_addresses_raw']
    self._data['or_addresses_raw'] = self._data['or_addresses']
    or_address_primary = self._data['or_addresses'][:1]
    # subsequent entries in the or_addresses array are in an arbitrary order
    # so we stabilise the addresses by sorting them in string order
    or_addresses_secondaries_stable = sorted(self._data['or_addresses'][1:])
    or_addresses_stable = or_address_primary + or_addresses_secondaries_stable
    self._data['or_addresses'] = or_addresses_stable

  def get_fingerprint(self):
    return self._fpr

  # is_valid_ipv[46]_address by gsathya, karsten, 2013
  @staticmethod
  def is_valid_ipv4_address(address):
    if not isinstance(address, (str, unicode)):
      return False

    # check if there are four period separated values
    if address.count(".") != 3:
      return False

    # checks that each value in the octet are decimal values between 0-255
    for entry in address.split("."):
      if not entry.isdigit() or int(entry) < 0 or int(entry) > 255:
        return False
      elif entry[0] == "0" and len(entry) > 1:
        return False  # leading zeros, for instance in "1.2.3.001"

    return True

  @staticmethod
  def is_valid_ipv6_address(address):
    if not isinstance(address, (str, unicode)):
      return False

    # remove brackets
    address = address[1:-1]

    # addresses are made up of eight colon separated groups of four hex digits
    # with leading zeros being optional
    # https://en.wikipedia.org/wiki/IPv6#Address_format

    colon_count = address.count(":")

    if colon_count > 7:
      return False  # too many groups
    elif colon_count != 7 and not "::" in address:
      return False  # not enough groups and none are collapsed
    elif address.count("::") > 1 or ":::" in address:
      return False  # multiple groupings of zeros can't be collapsed

    found_ipv4_on_previous_entry = False
    for entry in address.split(":"):
      # If an IPv6 address has an embedded IPv4 address,
      # it must be the last entry
      if found_ipv4_on_previous_entry:
        return False
      if not re.match("^[0-9a-fA-f]{0,4}$", entry):
        if not Candidate.is_valid_ipv4_address(entry):
          return False
        else:
          found_ipv4_on_previous_entry = True

    return True

  def _compute_orport(self):
    # Choose the first ORPort that's on the same IPv4 address as the DirPort.
    # In rare circumstances, this might not be the primary ORPort address.
    # However, _stable_sort_or_addresses() ensures we choose the same one
    # every time, even if onionoo changes the order of the secondaries.
    (diripaddr, dirport) = self._data['dir_address'].split(':', 2)
    self.orport = None
    for i in self._data['or_addresses']:
      if i != self._data['or_addresses'][0]:
        logging.debug('Secondary IPv4 Address Used for %s: %s'%(self._fpr, i))
      (ipaddr, port) = i.rsplit(':', 1)
      if (ipaddr == diripaddr) and Candidate.is_valid_ipv4_address(ipaddr):
        self.orport = int(port)
        return

  def _compute_ipv6addr(self):
    # Choose the first IPv6 address that uses the same port as the ORPort
    # Or, choose the first IPv6 address in the list
    # _stable_sort_or_addresses() ensures we choose the same IPv6 address
    # every time, even if onionoo changes the order of the secondaries.
    self.ipv6addr = None
    # Choose the first IPv6 address that uses the same port as the ORPort
    for i in self._data['or_addresses']:
      (ipaddr, port) = i.rsplit(':', 1)
      if (port == self.orport) and Candidate.is_valid_ipv6_address(ipaddr):
        self.ipv6addr = ipaddr
        return
    # Choose the first IPv6 address in the list
    for i in self._data['or_addresses']:
      (ipaddr, port) = i.rsplit(':', 1)
      if Candidate.is_valid_ipv6_address(ipaddr):
        self.ipv6addr = ipaddr
        return

  @staticmethod
  def _extract_generic_history(history, which='unknown'):
    # given a tree like this:
    #   {
    #     "1_month": {
    #         "count": 187,
    #         "factor": 0.001001001001001001,
    #         "first": "2015-02-27 06:00:00",
    #         "interval": 14400,
    #         "last": "2015-03-30 06:00:00",
    #         "values": [
    #             999,
    #             999
    #         ]
    #     },
    #     "1_week": {
    #         "count": 169,
    #         "factor": 0.001001001001001001,
    #         "first": "2015-03-23 07:30:00",
    #         "interval": 3600,
    #         "last": "2015-03-30 07:30:00",
    #         "values": [ ...]
    #     },
    #     "1_year": {
    #         "count": 177,
    #         "factor": 0.001001001001001001,
    #         "first": "2014-04-11 00:00:00",
    #         "interval": 172800,
    #         "last": "2015-03-29 00:00:00",
    #         "values": [ ...]
    #     },
    #     "3_months": {
    #         "count": 185,
    #         "factor": 0.001001001001001001,
    #         "first": "2014-12-28 06:00:00",
    #         "interval": 43200,
    #         "last": "2015-03-30 06:00:00",
    #         "values": [ ...]
    #     }
    #   },
    # extract exactly one piece of data per time interval,
    # using smaller intervals where available.
    #
    # returns list of (age, length, value) dictionaries.

    generic_history = []

    periods = history.keys()
    periods.sort(key = lambda x: history[x]['interval'])
    now = datetime.datetime.now()
    newest = now
    for p in periods:
      h = history[p]
      interval = datetime.timedelta(seconds = h['interval'])
      this_ts = parse_ts(h['last'])

      if (len(h['values']) != h['count']):
        logging.warn('Inconsistent value count in %s document for %s'
                     %(p, which))
      for v in reversed(h['values']):
        if (this_ts <= newest):
          generic_history.append(
            { 'age': (now - this_ts).total_seconds(),
              'length': interval.total_seconds(),
              'value': v
            })
          newest = this_ts
        this_ts -= interval

      if (this_ts + interval != parse_ts(h['first'])):
        logging.warn('Inconsistent time information in %s document for %s'
                     %(p, which))

    #print json.dumps(generic_history, sort_keys=True,
    #                  indent=4, separators=(',', ': '))
    return generic_history

  @staticmethod
  def _avg_generic_history(generic_history):
    a = []
    for i in generic_history:
      w = i['length'] * math.pow(AGE_ALPHA, i['age']/(3600*24))
      a.append( (i['value'] * w, w) )

    sv = math.fsum(map(lambda x: x[0], a))
    sw = math.fsum(map(lambda x: x[1], a))

    return sv/sw

  def _add_generic_history(self, history):
    periods = r['read_history'].keys()
    periods.sort(key = lambda x: r['read_history'][x]['interval'] )

    print periods

  def add_running_history(self, history):
    pass

  def add_uptime(self, uptime):
    logging.debug('Adding uptime %s.'%(self._fpr,))

    # flags we care about: Running, V2Dir, Guard
    if not 'flags' in uptime:
      logging.debug('No flags in document for %s.'%(self._fpr,))
      return

    for f in ['Running', 'Guard', 'V2Dir']:
      if not f in uptime['flags']:
        logging.debug('No %s in flags for %s.'%(f, self._fpr,))
        return

    running = self._extract_generic_history(uptime['flags']['Running'],
                                            '%s-Running'%(self._fpr))
    guard = self._extract_generic_history(uptime['flags']['Guard'],
                                          '%s-Guard'%(self._fpr))
    v2dir = self._extract_generic_history(uptime['flags']['V2Dir'],
                                          '%s-V2Dir'%(self._fpr))
    if 'BadExit' in uptime['flags']:
      badexit = self._extract_generic_history(uptime['flags']['BadExit'],
                                              '%s-BadExit'%(self._fpr))

    self._running = self._avg_generic_history(running) / ONIONOO_SCALE_ONE
    self._guard = self._avg_generic_history(guard) / ONIONOO_SCALE_ONE
    self._v2dir = self._avg_generic_history(v2dir) / ONIONOO_SCALE_ONE
    self._badexit = None
    if 'BadExit' in uptime['flags']:
      self._badexit = self._avg_generic_history(badexit) / ONIONOO_SCALE_ONE

  def is_candidate(self):
    if (self._data['last_changed_address_or_port'] >
        self.CUTOFF_ADDRESS_AND_PORT_STABLE):
      logging.debug('%s not a candidate: changed address/port recently (%s)',
        self._fpr, self._data['last_changed_address_or_port'])
      return False
    if self._running < CUTOFF_RUNNING:
      logging.debug('%s not a candidate: running avg too low (%lf)',
                    self._fpr, self._running)
      return False
    if self._guard < CUTOFF_GUARD:
      logging.debug('%s not a candidate: guard avg too low (%lf)',
                    self._fpr, self._guard)
      return False
    if self._v2dir < CUTOFF_V2DIR:
      logging.debug('%s not a candidate: v2dir avg too low (%lf)',
                    self._fpr, self._v2dir)
      return False
    if self._badexit is not None and self._badexit > PERMITTED_BADEXIT:
      logging.debug('%s not a candidate: badexit avg too high (%lf)',
                    self._fpr, self._badexit)
      return False
    # if the relay doesn't report a version, also exclude the relay
    if (not self._data.has_key('recommended_version')
        or not self._data['recommended_version']):
      return False
    return True

  def fallback_weight_fraction(self, total_weight):
    return float(self._data['consensus_weight']) / total_weight

  # return the original consensus weight, if it exists,
  # or, if not, return the consensus weight
  def original_consensus_weight(self):
    if self._data.has_key('original_consensus_weight'):
      return self._data['original_consensus_weight']
    else:
      return self._data['consensus_weight']

  def original_fallback_weight_fraction(self, total_weight):
    return float(self.original_consensus_weight()) / total_weight

  def fallbackdir_line(self, total_weight, original_total_weight):
    # /*
    # nickname
    # weight / total (percentage)
    # [original weight / original total (original percentage)]
    # [contact]
    # */
    # "address:port orport=port id=fingerprint"
    # "[ipv6=addr]"
    # "weight=num",
    # Multiline C comment
    s = '/*'
    s += '\n'
    s += cleanse_c_multiline_comment(self._data['nickname'])
    s += '\n'
    weight = self._data['consensus_weight']
    percent_weight = self.fallback_weight_fraction(total_weight)*100
    s += 'Fallback Weight: %d / %d (%.3f%%)'%(weight, total_weight,
                                              percent_weight)
    s += '\n'
    o_weight = self.original_consensus_weight()
    if o_weight != weight:
      o_percent_weight = self.original_fallback_weight_fraction(
                                                     original_total_weight)*100
      s += 'Consensus Weight: %d / %d (%.3f%%)'%(o_weight,
                                                 original_total_weight,
                                                 o_percent_weight)
      s += '\n'
    if self._data['contact'] is not None:
      s += cleanse_c_multiline_comment(self._data['contact'])
      s += '\n'
    s += '*/'
    s += '\n'
    # Multi-Line C string with trailing comma (part of a string list)
    # This makes it easier to diff the file, and remove IPv6 lines using grep
    # Integers don't need escaping
    s += '"%s orport=%d id=%s"'%(
            cleanse_c_string(self._data['dir_address']),
            self.orport,
            cleanse_c_string(self._fpr))
    s += '\n'
    if self.ipv6addr is not None:
      s += '" ipv6=%s"'%(
            cleanse_c_string(self.ipv6addr))
      s += '\n'
    s += '" weight=%d",'%(weight)
    return s

class CandidateList(dict):
  def __init__(self):
    pass

  def _add_relay(self, details):
    if not 'dir_address' in details: return
    c = Candidate(details)
    self[ c.get_fingerprint() ] = c

  def _add_uptime(self, uptime):
    try:
      fpr = uptime['fingerprint']
    except KeyError:
      raise Exception("Document has no fingerprint field.")

    try:
      c = self[fpr]
    except KeyError:
      logging.debug('Got unknown relay %s in uptime document.'%(fpr,))
      return

    c.add_uptime(uptime)

  def _add_details(self):
    logging.debug('Loading details document.')
    d = fetch('details',
        fields=('fingerprint,nickname,contact,last_changed_address_or_port,' +
              'consensus_weight,or_addresses,dir_address,recommended_version'))
    logging.debug('Loading details document done.')

    if not 'relays' in d: raise Exception("No relays found in document.")

    for r in d['relays']: self._add_relay(r)

  def _add_uptimes(self):
    logging.debug('Loading uptime document.')
    d = fetch('uptime')
    logging.debug('Loading uptime document done.')

    if not 'relays' in d: raise Exception("No relays found in document.")
    for r in d['relays']: self._add_uptime(r)

  def add_relays(self):
    self._add_details()
    self._add_uptimes()

  # Find fallbacks that fit the uptime, stability, and flags criteria
  def compute_fallbacks(self):
    self.fallbacks = map(lambda x: self[x],
                      sorted(
                        filter(lambda x: self[x].is_candidate(),
                               self.keys()),
                        key=lambda x: self[x]._data['consensus_weight'],
                        reverse=True)
                      )

  # Remove any fallbacks in excess of MAX_FALLBACK_COUNT,
  # starting with the lowest-weighted fallbacks
  # total_weight should be recalculated after calling this
  def exclude_excess_fallbacks(self):
    self.fallbacks = self.fallbacks[:MAX_FALLBACK_COUNT]

  # Reweight any fallbacks with weights higher than MAX_WEIGHT_FRACTION
  # total_weight is kept constant
  # fallbacks are kept sorted, although high weights will be reduced to
  # equal maximum acceptable weights
  # This is complex, let's check the results carefully if it's ever needed
  def reweight_high_weight_fallbacks(self, total_weight):
    if MAX_WEIGHT_FRACTION * len(self.fallbacks) < 1.0:
      raise Exception(
        'Max Fallback Weight %.3f%% is unachievable '%(MAX_WEIGHT_FRACTION) +
        'with Current Fallback Count %d.'%(len(self.fallbacks)))
    relays_reduced = 0
    relays_increased = 0
    total_excess_weight = 0
    # the total weight of relays remaining
    total_weight_remaining = total_weight
    max_acceptable_weight = total_weight * MAX_WEIGHT_FRACTION
    for f in self.fallbacks:
      frac_weight = f.fallback_weight_fraction(total_weight)
      if frac_weight > MAX_WEIGHT_FRACTION:
        relays_reduced += 1
        current_weight = f._data['consensus_weight']
        excess_weight = current_weight - max_acceptable_weight
        f._data['original_consensus_weight'] = current_weight
        f._data['consensus_weight'] = max_acceptable_weight
        total_excess_weight += excess_weight
        total_weight_remaining -= current_weight
    # avoid the second loop if we have no weight to reassign
    if total_excess_weight == 0:
      return (0, 0, 0)
    remaining_excess_weight = total_excess_weight
    # we increase each fallback's weight proportional to its current weight,
    # up to max_acceptable_weight
    for f in self.fallbacks:
      frac_weight = f.fallback_weight_fraction(total_weight)
      if frac_weight < MAX_WEIGHT_FRACTION:
        relays_increased += 1
        current_weight = f._data['consensus_weight']
        frac_weight_no_excess = f.fallback_weight_fraction(
                                    total_weight_remaining)
        additional_weight = frac_weight_no_excess * remaining_excess_weight
        # cap the final weight at max_acceptable_weight
        new_weight = current_weight + additional_weight
        if new_weight > max_acceptable_weight:
          new_weight = max_acceptable_weight
          additional_weight = new_weight - current_weight
        f._data['original_consensus_weight'] = current_weight
        f._data['consensus_weight'] = new_weight
        remaining_excess_weight -= additional_weight
        total_weight_remaining -= new_weight
    # check we redistributed all the weight
    if remaining_excess_weight > 0:
      raise Exception(
        'Leftover weight after reweighting high weight fallbacks: %d. '
        %(remaining_excess_weight) +
        'Try increasing TARGET_MAX_WEIGHT_FRACTION.')
    return (relays_reduced, relays_increased, total_excess_weight)

  # Remove any fallbacks with weights lower than MIN_WEIGHT_FRACTION
  # total_weight should be recalculated after calling this
  def exclude_low_weight_fallbacks(self, total_weight):
    self.fallbacks = filter(
            lambda x:
             x.fallback_weight_fraction(total_weight) >= MIN_WEIGHT_FRACTION,
             self.fallbacks)

  def fallback_weight_total(self):
    return sum(f._data['consensus_weight'] for f in self.fallbacks)

  def fallback_min_weight(self):
    return self.fallbacks[-1]

  def fallback_max_weight(self):
    return self.fallbacks[0]

  def summarise_fallbacks(self, eligible_count, eligible_weight,
                          relays_reduced, relays_increased, excess_weight):
    # Report:
    #  the number of fallback directories (with min & max limits);
    #    #error if below minimum count
    #  the total weight, min & max fallback proportions
    #    #error if outside max weight proportion
    # Multiline C comment with #error if things go bad
    s = '/*'
    s += '\n'
    s += 'Fallback Directory Summary'
    s += '\n'
    # Integers don't need escaping in C comments
    fallback_count = len(self.fallbacks)
    s += 'Final Count:  %d (Eligible %d, Clamped to %d)'%(
            fallback_count,
            eligible_count,
            MAX_FALLBACK_COUNT)
    s += '\n'
    if fallback_count < MIN_FALLBACK_COUNT:
      s += '*/'
      s += '\n'
      # We must have a minimum number of fallbacks so they are always
      # reachable, and are in diverse locations
      s += '#error Fallback Count %d is too low. '%(fallback_count)
      s += 'Must be at least %d for diversity.'%(MIN_FALLBACK_COUNT)
      s += '\n'
      s += '/*'
      s += '\n'
    total_weight = self.fallback_weight_total()
    min_fb = self.fallback_min_weight()
    min_weight = min_fb._data['consensus_weight']
    min_percent = min_fb.fallback_weight_fraction(total_weight)*100.0
    max_fb = self.fallback_max_weight()
    max_weight = max_fb._data['consensus_weight']
    max_frac = max_fb.fallback_weight_fraction(total_weight)
    max_percent = max_frac*100.0
    s += 'Final Weight: %d (Eligible %d)'%(total_weight, eligible_weight)
    s += '\n'
    s += 'Max Weight:   %d (%.3f%%) (Clamped to %.3f%%)'%(
                                                max_weight,
                                                max_percent,
                                                TARGET_MAX_WEIGHT_FRACTION*100)
    s += '\n'
    s += 'Min Weight:   %d (%.3f%%) (Clamped to %.3f%%)'%(
                                                min_weight,
                                                min_percent,
                                                MIN_WEIGHT_FRACTION*100)
    s += '\n'
    if eligible_count != fallback_count:
      s += 'Excluded:     %d (Excess or Low Weight)'%(
                                              eligible_count - fallback_count)
      s += '\n'
    if relays_reduced > 0:
      s += 'Reweighted:   %d (%.3f%%) Excess Weight, '%(
                                    excess_weight,
                                    (100.0 * excess_weight) / total_weight)
      s += '%d High Weight Fallbacks (%.1f%%)'%(
                                    relays_reduced,
                                    (100.0 * relays_reduced) / fallback_count)
      s += '\n'
    s += '*/'
    if max_frac > TARGET_MAX_WEIGHT_FRACTION:
      s += '\n'
      # We must restrict the maximum fallback weight, so an adversary
      # at or near the fallback doesn't see too many clients
      s += '#error Max Fallback Weight %.3f%% is too high. '%(max_frac*100)
      s += 'Must be at most %.3f%% for client anonymity.'%(
                                                TARGET_MAX_WEIGHT_FRACTION*100)
    return s

def list_fallbacks():
  """ Fetches required onionoo documents and evaluates the
      fallback directory criteria for each of the relays """

  candidates = CandidateList()
  candidates.add_relays()
  candidates.compute_fallbacks()
  eligible_count = len(candidates.fallbacks)
  eligible_weight = candidates.fallback_weight_total()

  # print the raw fallback list
  #total_weight = candidates.fallback_weight_total()
  #for x in candidates.fallbacks:
  #  print x.fallbackdir_line(total_weight, total_weight)

  # When candidates are excluded, total_weight decreases, and
  # the proportional weight of other candidates increases.
  candidates.exclude_excess_fallbacks()
  total_weight = candidates.fallback_weight_total()

  # When candidates are reweighted, total_weight remains constant.
  # Previously low-weight candidates might obtain sufficient weights.
  # Save the weight at which we reweighted fallbacks for the summary
  reweighted_total_weight = total_weight
  (reduced, increased, excess) = candidates.reweight_high_weight_fallbacks(
                                                                  total_weight)

  # When candidates are excluded, total_weight decreases, and
  # the proportional weight of other candidates increases.
  # No new low weight candidates will be created during exclusions.
  # However, high weight candidates may increase over the maximum.
  # This should not be an issue, except in pathological cases.
  candidates.exclude_low_weight_fallbacks(total_weight)
  total_weight = candidates.fallback_weight_total()

  # check we haven't exceeded TARGET_MAX_WEIGHT_FRACTION
  # since reweighting preserves the orginal sort order,
  # the maximum weights will be at the head of the list
  max_weight_fb = candidates.fallback_max_weight()
  max_weight = max_weight_fb.fallback_weight_fraction(total_weight)
  if  max_weight > TARGET_MAX_WEIGHT_FRACTION:
    raise Exception(
    'Maximum fallback weight: %.3f%% exceeds target %.3f%%. '%(
                                                    max_weight,
                                                    TARGET_MAX_WEIGHT_FRACTION
                                                    ) +
    'Try decreasing REWEIGHTING_FUDGE_FACTOR.')

  print candidates.summarise_fallbacks(eligible_count, eligible_weight,
                                       reduced, increased, excess)

  for s in fetch_source_list():
    print describe_fetch_source(s)

  for x in candidates.fallbacks[:MAX_FALLBACK_COUNT]:
    print x.fallbackdir_line(total_weight, reweighted_total_weight)
    #print json.dumps(candidates[x]._data, sort_keys=True, indent=4,
    #                  separators=(',', ': '), default=json_util.default)

if __name__ == "__main__":
  list_fallbacks()
