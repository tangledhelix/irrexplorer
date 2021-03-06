#!/usr/bin/env python
# Copyright (c) 2015, Job Snijders
#
# This file is part of IRR Explorer
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are met:
#
# 1. Redistributions of source code must retain the above copyright notice,
# this list of conditions and the following disclaimer.
#
# 2. Redistributions in binary form must reproduce the above copyright notice,
# this list of conditions and the following disclaimer in the documentation
# and/or other materials provided with the distribution.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
# AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
# IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
# ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE
# LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR
# CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF
# SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS
# INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN
# CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE)
# ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
# POSSIBILITY OF SUCH DAMAGE.

from irrexplorer import config
from irrexplorer import nrtm
from irrexplorer import ripe
from irrexplorer import bgp
from irrexplorer import utils

import time
import ipaddr
import threading
import multiprocessing
import radix
import json

from flask import Flask, render_template, request, flash, redirect, \
    url_for, abort
from flask_bootstrap import Bootstrap
from flask_wtf import Form
from wtforms import TextField, SubmitField
from wtforms.validators import Required



class NoPrefixError(Exception):
    pass



class LookupWorker(threading.Thread):
    def __init__(self, tree, asn_prefix_map, assets,
                 lookup_queue, result_queue):
        threading.Thread.__init__(self)
        self.tree = tree
        self.lookup_queue = lookup_queue
        self.result_queue = result_queue
        self.asn_prefix_map = asn_prefix_map
        self.assets = assets

    def run(self):
        while True:
            lookup, target = self.lookup_queue.get()
            results = {}
            if not lookup:
                continue

            if lookup == "search_specifics":
                data = None
                for rnode in self.tree.search_covered(target):
                    prefix = rnode.prefix
                    origins = rnode.data['origins']
                    results[prefix] = {}
                    results[prefix]['origins'] = origins
                self.result_queue.put(results)

            elif lookup == "search_aggregate":
                rnode = self.tree.search_worst(target)
                if not rnode:
                    self.result_queue.put(None)
                else:
                    prefix = rnode.prefix
                    data = rnode.data
                    self.result_queue.put((prefix, data))

            elif lookup == "search_exact":
                rnode = self.tree.search_exact(target)
                if not rnode:
                    self.result_queue.put({})
                else:
                    prefix = rnode.prefix
                    origins = rnode.data['origins']
                    results[prefix] = {}
                    results[prefix]['origins'] = origins
                    self.result_queue.put(results)

            elif lookup == "inverseasn":
                if target in self.asn_prefix_map:
                    self.result_queue.put(self.asn_prefix_map[target])
                else:
                    self.result_queue.put([])

            elif lookup == "asset_search":
                if target in self.assets:
                    self.result_queue.put(self.assets[target])
                else:
                    self.result_queue.put([])

            self.lookup_queue.task_done()


class NRTMWorker(multiprocessing.Process):
    """
    Launches an nrtm.client() instance and feeds the output in to a
    radix tree. Somehow allow other processes to lookup entries in the
    radix tree. Destroy & rebuild radix tree when serial overruns and
    a new connection must be established with the NRTM host.
    """
    def __init__(self, feedconfig, lookup_queue, result_queue):
        """
        Constructor.
        @param config dict() with NRTM host information
        @param nrtm_queue Queue() where NRTM output goes
        """
        multiprocessing.Process.__init__(self)
        self.feedconfig = feedconfig
        self.lookup_queue = lookup_queue
        self.result_queue = result_queue
        self.tree = radix.Radix()
        self.dbname = feedconfig['dbname']
        self.asn_prefix_map = {}
        self.assets = {}
        self.ready_event = multiprocessing.Event()
        self.lookup = LookupWorker(self.tree, self.asn_prefix_map, self.assets,
                                   self.lookup_queue, self.result_queue)

# TODO
# add completly new rnode from irr
# add completly new rnode from bgp
# add more data to existing rnode
# remove data from existing rnode
# remove existing rnode (last bgp or irr withdraw)

    def run(self):
        """
        Process run method, fetch NRTM updates and put them in the
        a radix tree.
        """
        self.lookup.setDaemon(True)
        self.lookup.start()

        self.feed = nrtm.client(**self.feedconfig)
        while True:
            for cmd, serial, obj in self.feed.get():
                if not obj:
                    continue
                try:
                    if not self.dbname == obj['source']:
                        """ ignore updates for which the source does not
                        match the configured/expected database """
                        continue
                except KeyError:
                    print "ERROR: NRTM object without source in %s: %s" % (self.dbname, obj)
                    continue

                if obj['kind'] in ["route", "route6"]:
                    if cmd == "ADD":
                        try:
                            ipaddr.IPNetwork(obj['name'])
                        except ValueError:
                            print "ERROR: non-valid stuff in %s: %s" \
                                % (self.dbname, obj)
                            continue
                        if not self.tree.search_exact(obj['name']):
                            # FIXME does sometimes fails in the pure python
                            # py-radix
                            rnode = self.tree.add(obj['name'])
                            rnode.data['origins'] = [obj['origin']]
                        else:
                            rnode = self.tree.search_exact(obj['name'])
                            rnode.data['origins'] = list(set([obj['origin']] + list(rnode.data['origins'])))

                        # add prefix to the inverse ASN map
                        if obj['origin'] not in self.asn_prefix_map:
                            self.asn_prefix_map[obj['origin']] = [obj['name']]
                        else:
                            self.asn_prefix_map[obj['origin']].append(obj['name'])
                    else:
                        try:
                            self.tree.delete(obj['name'])
                            self.asn_prefix_map[obj['origin']].remove(obj['name'])
                        except KeyError:
                            print "ERROR: Could not remove object from the tree in %s: %s" % (self.dbname, obj)

                if obj['kind'] == "as-set":
                    if cmd == "ADD":
                        self.assets[obj['name']] = obj['members']
                    else:
                        del self.assets[obj['name']]

        print 'Done parsing database %s' % self.dbname
        self.ready_event.set()



databases = config('irrexplorer_config.yml').databases
lookup_queues = {}
result_queues = {}

nrtm_workers = []

for dbase in databases:
    name = dbase.keys()[0]
    feedconfig = dbase[name]
    feedconfig = dict(d.items()[0] for d in feedconfig)
    lookup_queues[name] = multiprocessing.JoinableQueue()
    result_queues[name] = multiprocessing.JoinableQueue()
    worker = NRTMWorker(feedconfig, lookup_queues[name], result_queues[name])
    worker.start()
    nrtm_workers.append(worker)

# Launch helper processes for BGP & RIPE managed space lookups
for q in ['RIPE-AUTH', 'BGP']:
    lookup_queues[q] = multiprocessing.JoinableQueue()
    result_queues[q] = multiprocessing.JoinableQueue()

bgp_worker = bgp.BGPWorker(lookup_queues['BGP'], result_queues['BGP'])
bgp_worker.start()

ripe_worker = ripe.RIPEWorker(lookup_queues['RIPE-AUTH'], result_queues['RIPE-AUTH'])
ripe_worker.start()
ripe_worker.ready_event.wait() # instant with current code

# wait until all workers are ready
for idx, nw in  enumerate(nrtm_workers):
    # nw.ready_event.wait()
    # somehow just wait() doesn't work, but with timeout it does...
    if nw.ready_event.wait(10):
        print 'NRTM worker %i ready' % idx
    else:
        print 'NRTM worker %i still waiting' % idx

print 'All NRTM workers ready'

bgp_worker.ready_event.wait()
print 'BGP worker ready, continuing'


def irr_query(query_type, target):
    global lookup_queues
    global result_queues
    for i in lookup_queues:
        if i in ['BGP', 'RIPE-AUTH']:
            continue
        print "doing lookup for %s in %s" % (target, i)
        lookup_queues[i].put((query_type, target))
    for i in lookup_queues:
        if i in ['BGP', 'RIPE-AUTH']:
            continue
        lookup_queues[i].join()
    result = {}
    for i in result_queues:
        if i in ['BGP', 'RIPE-AUTH']:
            continue
        result[i] = result_queues[i].get()
        result_queues[i].task_done()
    return result

def other_query(data_source, query_type, target):
    global lookup_queues
    global result_queues
    lookup_queues[data_source].put((query_type, target))
    lookup_queues[data_source].join()
    result = result_queues[data_source].get()
    result_queues[data_source].task_done()
    return result


def bgp_query():
    pass


def ripe_query():
    pass


IRR_DBS = ['afrinic', 'altdb', 'apnic', 'arin', 'bboi', 'bell', 'gt', 'jpirr', 'level3', 'nttcom', 'radb', 'rgnet', 'savvis', 'tc', 'ripe']

IRR_DBS_EXCEPT_RIPE = IRR_DBS[:]
IRR_DBS_EXCEPT_RIPE.remove('ripe')


def prefix_post_process(prefixes):

    # build list of databases with no relevant information
    db_entries = {}

    for pfi in prefixes.values():
        for db, info in pfi.items():
            if db in IRR_DBS: # skip advice, ripe managed, etc
                db_entries.setdefault(db, []).append( bool(info) )

    db_truncate = [ db for db, dbil in db_entries.items() if any(dbil) is False ]
    print 'db truncate', db_truncate

    # remove databases with no relevant data from result
    for pfi in prefixes.values():
        for db in db_truncate:
            if db in pfi: # less code than try+except
                pfi.pop(db)

    # create list of database with information
    db_info = db_entries.keys()
    for db in db_truncate:
        db_info.remove(db)

    print 'db info', db_info

    # make some nice blanks
    for prefix in prefixes:
        for db in db_info:
            if not db in prefixes[prefix] or not prefixes[prefix][db]:
                prefixes[prefix][db] = "-"

    msg = 'No relevant information databases %s' % str( ' '.join(db_truncate) )

    return prefixes, msg



def prefix_report(prefix, exact=False):
    """
        - find least specific
        - search in BGP for more specifics
        - search in IRR for more specifics
        - check all prefixes whether they are RIPE managed or not
        - return dict
    """

    t_start = time.time()

    if exact:
        bgp_specifics = other_query("BGP", "search_exact", prefix)
        irr_specifics = irr_query("search_exact", prefix)
    else:
        tree = radix.Radix()
        bgp_aggregate = other_query("BGP", "search_aggregate", prefix)
        if bgp_aggregate:
            bgp_aggregate = bgp_aggregate[0]
            tree.add(bgp_aggregate)
        irr_aggregate = irr_query("search_aggregate", prefix)
        for r in irr_aggregate:
            if irr_aggregate[r]:
                tree.add(irr_aggregate[r][0])
        aggregate = tree.search_worst(prefix)
        if not aggregate:
            raise NoPrefixError("Could not find any matching prefix in IRR or BGP tables for %s" % prefix)

        aggregate = aggregate.prefix

        bgp_specifics = other_query("BGP", "search_specifics", aggregate)
        irr_specifics = irr_query("search_specifics", aggregate)

    prefixes = {}
    for p in bgp_specifics:
        if p not in prefixes:
            prefixes[p] = {'bgp_origin': bgp_specifics[p]['origins']}
        else:
            prefixes[p]['bgp_origin'] = bgp_specifics[p]['origins']
    for db in irr_specifics:
        if irr_specifics[db]:
            for p in irr_specifics[db]:
                if p not in prefixes:
                    prefixes[p] = {}
                    prefixes[p]['bgp_origin'] = False

    """
    irr_specifics looks like:
        {'apnic': {}, 'gt': {}, 'bboi': {}, 'radb': {}, 'jpirr': {},
        'bell': {}, 'altdb': {}, 'rgnet': {}, 'savvis': {}, 'level3': {},
        'ripe': {'85.184.0.0/16': {'origins': [8935]}},
        'arin': {}, 'afrinic': {}, 'tc': {}}
    """
    print "prefix_report: Lookup prefixes: %s" % prefixes
    print "prefix_report: Lookup irr_specifics: %s" % irr_specifics

    for db in irr_specifics:
        # set all db sources to False initially, later fill them
        for p in prefixes:
            prefixes[p][db] = False
        for p in irr_specifics[db]:
            prefixes.setdefault(p, {})[db] = irr_specifics[db][p]['origins']

    for p in prefixes:
        if other_query("RIPE-AUTH", "is_covered", p):
            prefixes[p]['ripe_managed'] = True
        else:
            prefixes[p]['ripe_managed'] = False

    # default, primary, succes, info, warning, danger
    for p in prefixes:
        print p
        print prefixes[p]

        anywhere = []
        for db in IRR_DBS:
            if not db in prefixes[p]:
                continue
            if prefixes[p][db]:
                for entry in prefixes[p][db]:
                    anywhere.append(entry)
        anywhere = list(set(anywhere))

        anywhere_not_ripe = []
        for db in IRR_DBS_EXCEPT_RIPE:
            if not db in prefixes[p]:
                continue
            if prefixes[p][db]:
                for entry in prefixes[p][db]:
                    anywhere_not_ripe.append(entry)
        anywhere_not_ripe = list(set(anywhere_not_ripe))

        if prefixes[p]['ripe_managed']:

            if prefixes[p]['ripe']:

                if prefixes[p]['bgp_origin'] in prefixes[p]['ripe']:

                    if len(anywhere) == 1 and prefixes[p]['bgp_origin'] not in anywhere_not_ripe:
                        prefixes[p]['advice'] = "Perfect"
                        prefixes[p]['label'] = "success"

                    elif prefixes[p]['bgp_origin'] == anywhere_not_ripe:
                        prefixes[p]['advice'] = "Proper RIPE DB object, but foreign or proxy objects also exist"
                        prefixes[p]['label'] = "warning"

                    elif prefixes[p]['bgp_origin'] in anywhere_not_ripe:
                        prefixes[p]['advice'] = "Proper RIPE DB object, but foreign objects also exist, consider removing these"
                        prefixes[p]['label'] = "warning"

                    else:
                        prefixes[p]['advice'] = "Looks good, but multiple entries exists in RIPE DB"
                        prefixes[p]['label'] = "success"

                elif prefixes[p]['bgp_origin']:
                    prefixes[p]['advice'] = "Prefix is in DFZ, but registered with wrong origin in RIPE!"
                    prefixes[p]['label'] = "danger"

                else:
                    # same as last else clause, not sure if this could actually be first
                    prefixes[p]['advice'] = "Not seen in BGP, but (legacy?) route-objects exist, consider clean-up"
                    prefixes[p]['label'] = "warning"

            else:   # no ripe registration

                if prefixes[p]['bgp_origin']:
                    prefixes[p]['advice'] = "Prefix is in DFZ, but NOT registered in RIPE!"
                    prefixes[p]['label'] = "danger"

                else:
                    prefixes[p]['advice'] = "Route objects in foreign registries exist, consider moving them to RIPE DB"
                    prefixes[p]['label'] = "warning"

        elif prefixes[p]['bgp_origin']: # not ripe managed

            if prefixes[p]['bgp_origin'] in anywhere:

                if len(anywhere) == 1:
                    prefixes[p]['advice'] = "Looks good: in BGP consistent origin AS in route-objects"
                    prefixes[p]['label'] = "success"
                else:
                    prefixes[p]['advice'] = "Multiple route-object exist with different origins"
                    prefixes[p]['label'] = 'warning'

            else:
                prefixes[p]['advice'] = "Prefix in DFZ, but no route-object with correct origin anywhere"
                prefixes[p]['label'] = "danger"

        else: # not ripe managed, no bgp origin
            prefixes[p]['advice'] = "Not seen in BGP, but (legacy?) route-objects exist, consider clean-up"
            prefixes[p]['label'] = "warning"


    prefixes, msg = prefix_post_process(prefixes)
    print msg # have to get this into the web page as well...


    t_delta = time.time() - t_start
    print
    print 'Time for prefix report for %s: %f' % (prefix, t_delta)
    print

    return prefixes


class InputForm(Form):
    field2 = TextField('Data', description='Input ASN, AS-SET or Prefix.',
                       validators=[Required()])
    submit_button = SubmitField('Submit')


def create_app(configfile=None):
    app = Flask(__name__)
    app.config.from_pyfile('appconfig.cfg')
    Bootstrap(app)

    @app.route('/', methods=['GET', 'POST'])
    def index_old():
        form = InputForm()
        if request.method == 'GET':
            return render_template('index.html', form=form)

        if request.method == 'POST':
            data = form.field2.data
            try:
                int(data)
                data = "AS%s" % data
            except ValueError:
                pass

            if utils.is_autnum(data):
                return redirect(url_for('autnum', autnum=data))

            elif utils.is_ipnetwork(data):
                flash('Just one field is required, fill it in!')
                return redirect(url_for('prefix_search', prefix=data))

#FIXME no support for as-set digging for now
#            elif data.startswith('AS'):
#                return redirect(url_for('asset', asset=data))

            else:
                return render_template('index.html', form=form)

    @app.route('/autnum/<autnum>')
    def autnum(autnum):
        return str(irr_query("inverseasn", autnum))

    @app.route('/prefix/<path:prefix>')
    @app.route('/prefix/', defaults={'prefix': None})
    @app.route('/prefix', defaults={'prefix': None})
    def prefix_search(prefix):
        return render_template('prefix.html')

    @app.route('/exact_prefix/<path:prefix>')
    @app.route('/exact_prefix/', defaults={'prefix': None})
    @app.route('/exact_prefix', defaults={'prefix': None})
    def exact_prefix_search(prefix):
        return render_template('exact_prefix.html')

    @app.route('/prefix_json/<path:prefix>')
    def prefix_json(prefix):
        try:
            ipaddr.IPNetwork(prefix)
        except ValueError:
            msg = 'Could not parse input %s as prefix' % prefix
            print msg
            abort(400, msg)
        try:
            prefix_data = prefix_report(prefix)
            return json.dumps(prefix_data)
        except NoPrefixError as e:
            print e
            abort(400, str(e))
        except Exception as e:
            print e
            msg = 'Error processing prefix %s: %s' % (prefix, str(e))
            print msg
            abort(500, msg)


    @app.route('/exact_prefix_json/<path:prefix>')
    def exact_prefix_json(prefix):
        try:
            ipaddr.IPNetwork(prefix)
        except ValueError:
            msg = 'Could not parse input %s as prefix' % prefix
            print msg
            abort(400, msg)

        try:
            prefix_data = prefix_report(prefix, exact=True)
            return json.dumps(prefix_data)
        except NoPrefixError as e:
            print e
            abort(400, str(e))
        except Exception as e:
            print e
            msg = 'Error processing prefix %s: %s' % (prefix, str(e))
            print msg
            abort(500, msg)


#    @app.route('/asset/<asset>')
#    def asset(asset):
#        return str(utils.lookup_assets(asset))

    return app

if __name__ == '__main__':
    create_app().run(host="0.0.0.0", debug=True, use_reloader=False)
