"""Tools for querying FDIC data.

First we import various modules and set ftool to be the tool we want:

>>> import fdic.query, json
>>> ftool = fdic.query.FDICTools


Now we can get the institutional data from the FDIC and sort it first
by assets and then by deposits. We print the result in JSON format
as a sanity check and see the largest 3 banks by assets:

>>> inst_data = ftool.get_sorted_inst_data()
>>> top_3_by_assets = inst_data[:3]
>>> print(json.dumps({i['NAME']: {n: i[n] for n in ['ASSET', 'DEP']}
...      for i in top_3_by_assets}, indent=2))
{
  "JPMorgan Chase Bank, National Association": {
    "ASSET": "3201942000",
    "DEP": "2440722000"
  },
  "Bank of America, National Association": {
    "ASSET": "2418508000",
    "DEP": "2042255000"
  },
  "Citibank, National Association": {
    "ASSET": "1766752000",
    "DEP": "1399631000"
  }
}


Next we pull in the Uniform Bank Performance Report data. Note that
you will have to download a zip file with this data first. If you
don't, you will get a NeedUBPRZipFile exception telling you how to
download the necessary file.

To save time in parsing, we can provide an rssd_filter to just
get data for the top 200 banks by assets. After getting the data,
we sort by the UBPRE569 field (unrealized losses as a percent of
tier 1 capital for held-to-maturity assets) and then print the data:

>>> ubpr_data = ftool.get_ubpr_inst_data(rssd_filter={
...     i['FED_RSSD'] for i in inst_data[:50]})
>>> htm_data = list(sorted(ubpr_data, key=lambda i: i['UBPRE569']))
>>> print(json.dumps({i['NAME']: {n: i[n] for n in ['NAME', 'UBPRE569']}
...      for i in htm_data[:3]}, indent=2))
{
  "Silicon Valley Bank": {
    "NAME": "Silicon Valley Bank",
    "UBPRE569": -89.2
  },
  "Bank of America, National Association": {
    "NAME": "Bank of America, National Association",
    "UBPRE569": -59.95
  },
  "Charles Schwab Bank, SSB": {
    "NAME": "Charles Schwab Bank, SSB",
    "UBPRE569": -46.87
  }
}

"""

import tempfile
import typing
import re
import zipfile
import logging
import csv
import os
import threading
import json
import requests


class NeedUBPRZipFile(Exception):
    """Excepiton to raise if UBPR Zip file is not present.
    """

    def __init__(self, ubpr_zip_file):
        msg = f'''No UBPR zip file found at {ubpr_zip_file}.

Please download the UBPR zip file and save it to the path
    {ubpr_zip_file}
or provide a path to your UBPR zip file.

You can download the UBPR zip file as follows:

  1. Go to https://cdr.ffiec.gov/public/PWS/DownloadBulkData.aspx
  2. Select "UBPR Ratio -- Single Period"
     - you can also select other periods if desired
  3. Choose XBRL format.
  4. Click download to get the file.

Either save the file to {ubpr_zip_file} or provide the path
where you save the file to various functions.
        '''
        super().__init__(msg)


class FDICTools:
    """Class to work with FDIC data.

    """

    INST_URL = (  # Lookup the link from https://banks.data.fdic.gov/docs/
        'https://s3-us-gov-west-1.amazonaws.com'
        '/cg-2e5c99a6-e282-42bf-9844-35f5430338a5'
        '/downloads/institutions.csv')
    API_ROOT = 'https://banks.data.fdic.gov/api'
    FDIC_INST_FILE = os.environ.get('FDIC_INST_FILE', None)
    _lock = threading.Lock()

    @classmethod
    def get_raw_ubpr_data(cls, ubpr_zip_file, rssd_filter=None, codes={
            'UBPRE567':  # OFF BALANCE SHEET/OVERALL RISK INDICATORS
            {'convert': float},
            'UBPRE568':  # UNREALIZED APPN/DEPN/OVERALL RISK INDICATORS
            {'convert': float},
            'UBPRE569':  # UNREAL APP/DEP % TIER ONE CAP/OVERALL RISK INDICATORS
            {'convert': float}
            }):
        """

        :param ubpr_zip_file:        

        :param rssd_filter=None:        

        :param codes:    

        ~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-

        :return:

        ~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-

        PURPOSE:  Parse data from bulk file for Uniform Bank Performance Report.
                  You will need to go to the URL for the UBPR bulk download
                  at https://cdr.ffiec.gov/public/PWS/DownloadBulkData.aspx,
                  select "UBPR Ratio -- Single Period", choose XBRL format,
                  and click download to get the file.

                  To find the codes, see the following URL:

            https://www.federalreserve.gov/apps/mdrm/data-dictionary/search/
              series?sid=1388&show_short_title=False&show_conf=False
              &rep_status=All&rep_state=Opened&rep_period=Before
              &date_start=20160912&date_end=20160912
        """
        file_re = re.compile('FFIEC CDR UBPR FI *'
                           '(?P<rssd>[0-9]+)'
                           '.ID RSSD. *'
                           '(?P<date>[0-9]{8})'
                           '.XBRL.xml')
        field_re = '(' + ')|('.join([
            f'(?P<o_{f}><uc:{f} [^>]+>)(?P<{f}>[^<]+)</uc:{f}>'
            for f in codes]) + ')'
        results = []
        with zipfile.ZipFile(ubpr_zip_file) as zarc:
            for item in zarc.infolist():
                match = file_re.search(item.filename)
                if not match:
                    if item.filename.lower() != 'readme.txt':
                        logging.warning('Strange file %s skipped', item.filename)
                    continue
                if rssd_filter and match.group('rssd') not in rssd_filter:
                    continue
                info = {'filename': item.filename, 'rssd': match.group('rssd'),
                        'date': match.group('date')}
                with zarc.open(item.filename) as zfd:
                    for match in re.finditer(
                            field_re, zfd.read().decode('utf8')):
                        for key, value in match.groupdict().items():
                            if value is not None:
                                assert key not in info
                                convert = codes.get(key, {}).get(
                                    'convert', lambda x: x)
                                info[key] = convert(value)
                results.append(info)
        return results

    @classmethod
    def get_ubpr_inst_data(cls, ubpr_zip_file=None, **kwargs):
        ubpr_zip_file = ubpr_zip_file or os.path.join(
            tempfile.gettempdir(), 'ubpr.zip')
        if not os.path.exists(ubpr_zip_file):
            raise NeedUBPRZipFile(ubpr_zip_file)
        inst_data = cls.get_sorted_inst_data()
        rssd_map = {i['FED_RSSD']: i for i in inst_data}
        ubpr_data = cls.get_raw_ubpr_data(ubpr_zip_file, **kwargs)
        for item in ubpr_data:
            i_data = rssd_map[item['rssd']]
            item.update(i_data)
        
        return ubpr_data
        
    
    @classmethod
    def _save_to_csv(cls, data_list: typing.List[dict], filename, field_map):
        with open(filename, 'w', encoding='utf8') as raw_fd:
            writer = csv.DictWriter(raw_fd, fieldnames=[
                field_map[k] for k in field_map])
            writer.writeheader()
            for item in data_list:
                writer.writerow({v: item[k] for k, v in field_map.items()})
        

    @classmethod
    def download_inst_file(cls, ifile):
        if not ifile:
            ifile = cls.FDIC_INST_FILE
        if not ifile:
            ifile = os.path.join(tempfile.gettempdir(), 'fdic_institutions.csv')
            logging.info('Using %s for institutions file location.', ifile)
            cls.ifile = ifile
        if os.path.exists(ifile):
            logging.info('Using existing institutions file at %s', ifile)
            return ifile
        logging.warning('Downloading FDIC institutions file to %s.', ifile)
        with cls._lock:  # lock so don't have multiple threads writing file
            req = requests.get(cls.INST_URL)
            assert req.status_code == 200, (
                f'Could not download institutions file from {cls.INST_URL}')
            with open(ifile, 'w', encoding='utf8') as raw_fd:
                raw_fd.write(req.text)
        return ifile
        
    @classmethod
    def get_sorted_inst_data(cls, ifile=None, sort_keys=('ASSET', 'DEP'),
                             ignore_inactive=True):
        ifile = ifile or cls.FDIC_INST_FILE
        if not ifile or not os.path.exist(ifile):
            ifile = cls.download_inst_file(ifile)
        with open(ifile) as fd:
            data = list(csv.DictReader(fd))
            if ignore_inactive:
                data = [i for i in data if i['INACTIVE'] not in (1, '1')]
            sorted_data = list(sorted(data, key=lambda i: [
                (float(i[k]) if i[k] else 0) for k in sort_keys],
                                      reverse=True))
            return sorted_data

    @classmethod
    def get_data(cls, cert,
                 fields = ('CERT', 'REPDTE', 'ASSET' ,' DEP', 'SCHA', 'SCAF',
                           'SCRDEBT', 'NAME', 'ID'),
                 as_json=True):
        url = f'{cls.API_ROOT}/financials'
        req = requests.get(url, params={
            'filters': f'CERT:{cert}',
            'fields': ','.join(fields),
            'sort_by': 'REPDTE',
            'sort_order': 'DESC',
            'limit': 10,
            'offset': 0,
            'agg_limit': 5,
            'format': 'json',
            'download': 'false',
            'filename': 'data_file'})
        assert req.status_code == 200
        if as_json:
            return req.json()
        return {'req': req}
                           

    @classmethod
    def save_data(cls, cert_list, save_dir):
        results = []
        for cert in cert_list:
            logging.info('Working on CERT=%s', cert)
            fname = os.path.join(save_dir, cert + '.json')            
            if os.path.exists(fname):
                logging.warning(
                    'Skip download data since file %s exists', fname)
                with open(fname) as fd:
                    full_data = json.load(fd)
            else:
                full_data = cls.get_data(cert)
                with open(fname, 'w', encoding='utf8') as fd:
                    json.dump(full_data, fd)
            recent_data = full_data['data'][0]['data']
            repdte, name, scha, scaf, scrdebt = [recent_data[n] for n in [
                'REPDTE', 'NAME', 'SCHA', 'SCAF', 'SCRDEBT']]
            ratio = 'unknown'
            if repdte < '20221231':
                logging.warning('Skip data since REPDTE too old: %s',
                                str(recent_data))
                continue
            try:
                ratio = scha/scrdebt
            except Exception as problem:
                ratio = problem
            recent_data['ratio'] = ratio                
            logging.info('Saved %s with ratio %s', name, ratio)
            results.append(recent_data)
        return results

    @classmethod
    def analyze_data(cls, cert_list, save_dir):
        cls.save_dir(cert_list, save_dir)
        for cert in cert_list:
            fname = os.path.join(save_dir, cert + '.json')            
            
            
