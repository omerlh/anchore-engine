import os
import re
import sys
import json
import uuid
import shutil
import struct
import tarfile
import logging
#import subprocess
from pkg_resources import resource_filename

import anchore_engine.services.common
import anchore_engine.auth.common
import anchore_engine.auth.skopeo_wrapper
from anchore.anchore_utils import read_kvfile_todict

try:
    from anchore_engine.subsys import logger
    # Separate logger for use during bootstrap when logging may not be fully configured
    from twisted.python import log
except:
    import logging
    logger = logging.getLogger(__name__)
    logger.setLevel("DEBUG")
    log = logger

def squash(unpackdir, layers):
    rootfsdir = unpackdir + "/rootfs"

    if os.path.exists(unpackdir + "/squashed.tar"):
        return (True)

    if not os.path.exists(rootfsdir):
        os.makedirs(rootfsdir)

    revlayer = list(layers)
    revlayer.reverse()

    squashtarfile = tarfile.open(unpackdir + '/squashed_tmp.tar', mode='w', format=tarfile.PAX_FORMAT)

    allfiles = {}
    lastexcludes = list()
    excludes = {}
    hlinks = {}
    hfiles = {}
    layerfiles = {}
    thetfile = {}

    for l in revlayer:
        htype, lfile = l.split(":",1)

        layertar = unpackdir + "/raw/"+lfile+".tar"
        layerfiles[l] = {}

        count = 0
        logger.debug("\tPass 1: " + str(layertar))
        layertarfile = tarfile.open(layertar, mode='r', format=tarfile.PAX_FORMAT)
        whpatt = re.compile(".*\.wh\..*")
        for member in layertarfile.getmembers():
            layerfiles[l][member.name] = True

            count = count + 1
            #if count % 100 == 0:
            #    logger.debug("layer file processed count: " + str([count, member.name, len(excludes.keys())]))

            # check if file member is a whiteout
            if whpatt.match(member.name):    
                fsub = re.sub(r"\.wh\.", "", member.name)

                # never include a whiteout file
                if member.name not in excludes:
                    excludes[member.name] = True

                if fsub not in allfiles:
                    # if whiteouted file is not in allfiles from a higher layer, it means the file should be removed in lower layer
                    if fsub not in excludes:
                        excludes[fsub] = True

            # check for exclusion cases
            skip = False
            if member.name in allfiles:
                skip = True
            else:
                if member.name in excludes.keys():
                    skip = True
                elif excludes:
                    # discover if file is in an excluded directory
                    dtoks = member.name.split("/")
                    for i in range(0, len(dtoks)):
                        dtok = '/'.join(dtoks[0:i])
                        if dtok in excludes:
                            skip = True
                            break
                            
            if not skip:
                allfiles[member.name] = True

                if member.isfile():
                    squashtarfile.addfile(member, layertarfile.extractfile(member))
                else:
                    try:
                        squashtarfile.addfile(member, layertarfile.extractfile(member))
                    except:
                        squashtarfile.addfile(member)
            else:
                pass

        layertarfile.close()

    squashtarfile.close()

    squashtar = os.path.join(unpackdir, "squashed_tmp.tar")

    logger.debug("\tPass 3: " + str(squashtar))

    #tarcmd = "bsdtar -C " + rootfsdir + " -x -p -f " + squashtar
    tarcmd = "tar -C " + rootfsdir + " -x -f " + squashtar
    logger.debug("untarring squashed tarball: " + str(tarcmd))

    try:
        rc, sout, serr = anchore_engine.services.common.run_command(tarcmd)
        if rc != 0:
            raise Exception("command failed: cmd="+str(tarcmd)+" exitcode="+str(rc)+" stdout="+str(sout).strip()+" stderr="+str(serr).strip())
        else:
            logger.debug("command succeeded: stdout="+str(sout).strip()+" stderr="+str(serr).strip())
    except Exception as err:
        logger.error("command failed with exception - " + str(err))
        raise err

    imageSize = os.path.getsize(squashtar)
    
    return (squashtar, imageSize)

def make_staging_dirs(rootdir):
    if not os.path.exists(rootdir):
        raise Exception("passed in root directory must exist ("+str(rootdir)+")")

    rando = str(uuid.uuid4())
    ret = {
        'unpackdir': os.path.join(rootdir, rando),
        'copydir': os.path.join(rootdir, rando, "raw"),
        'rootfs': os.path.join(rootdir, rando, "rootfs"),
        'outputdir': os.path.join(rootdir, rando, "output")
    }
    for k in ret.keys():
        try:
            if not os.path.exists(ret[k]):
                logger.debug("making dir: " + k + " : " + str(ret[k]))
                os.makedirs(ret[k])
        except Exception as err:
            raise Exception("unable to prep staging directory - exception: " + str(err))

    return(ret)

def delete_staging_dirs(staging_dirs):
    for k in staging_dirs.keys():
        try:
            if os.path.exists(staging_dirs[k]):
                logger.debug("removing dir: " + k + " : " + str(staging_dirs[k]))
                shutil.rmtree(staging_dirs[k])
        except Exception as err:
            raise Exception("unable to delete staging directory - exception: " + str(err))

    return(True)

def pull_image(staging_dirs, pullstring, registry_creds=[]):
    outputdir = staging_dirs['outputdir']
    unpackdir = staging_dirs['unpackdir']
    copydir = staging_dirs['copydir']

    user = pw = None
    registry_verify = False

    # extract user/pw/verify from registry_creds
    try:
        if registry_creds:
            image_info = anchore_engine.services.common.get_image_info(None, 'docker', pullstring, registry_lookup=False)
            user, pw, registry_verify = anchore_engine.auth.common.get_creds_by_registry(image_info['registry'], registry_creds=registry_creds)
    except Exception as err:
        raise err

    # download
    try:
        rc = anchore_engine.auth.skopeo_wrapper.download_image(pullstring, copydir, user=user, pw=pw, verify=registry_verify)
    except Exception as err:
        raise err


    return(True)

def get_image_metadata_v1(staging_dirs, imageDigest, imageId, manifest_data, dockerfile_contents="", dockerfile_mode=""):
    outputdir = staging_dirs['outputdir']
    unpackdir = staging_dirs['unpackdir']
    copydir = staging_dirs['copydir']

    docker_history = []
    layers = []
    dockerfile_mode = "Guessed"
    dockerfile_contents = dockerfile_contents
    imageArch = ""

    try:
        imageArch = manifest_data['architecture']
    except:
        imageArch = ""

    try:
        for fslayer in manifest_data['fsLayers']:
            layers.append(fslayer['blobSum'])
    except Exception as err:
        logger.error("cannot get layers - exception: " + str(err))
        raise err

    try:
        hfinal = []
        count=0
        for rawhel in manifest_data['history']:
            hel = json.loads(rawhel['v1Compatibility'])
            
            try:
                lsize = hel['Size']
            except:
                lsize = 0
            
            if hel['container_config']['Cmd']:
                lcreatedby = ' '.join(hel['container_config']['Cmd'])
            else:
                lcreatedby = ""
            
            lcreated = hel['created']
            lid = layers[count]
            count = count + 1
            hfinal.append(
                {
                    'Created': lcreated,
                    'CreatedBy': lcreatedby,
                    'Comment': '',
                    'Id': lid,
                    'Size': lsize,
                    'Tags': []
                }
            )

        docker_history = hfinal
        if hfinal:
            with open(os.path.join(unpackdir, "docker_history.json"), 'w') as OFH:
                OFH.write(json.dumps(hfinal))
    except Exception as err:
        logger.error("cannot construct history - exception: " + str(err))
        raise err

    if not dockerfile_contents:
        # get dockerfile_contents (translate history to guessed DF)
        # TODO 'FROM' guess?
        dockerfile_contents = "FROM scratch\n"
        for hel in docker_history:
            patt = re.match("^/bin/sh -c #\(nop\) +(.*)", hel['CreatedBy'])
            if patt:
                cmd = patt.group(1)
            else:
                cmd = "RUN " + hel['CreatedBy']
            dockerfile_contents = dockerfile_contents + cmd + "\n"        
        dockerfile_mode = "Guessed"
    elif not dockerfile_mode:
        dockerfile_mode = "Actual"

    layers.reverse()

    return(docker_history, layers, dockerfile_contents, dockerfile_mode, imageArch)

def get_image_metadata_v2(staging_dirs, imageDigest, imageId, manifest_data, dockerfile_contents="", dockerfile_mode=""):
    outputdir = staging_dirs['outputdir']
    unpackdir = staging_dirs['unpackdir']
    copydir = staging_dirs['copydir']

    rawlayers = manifest_data['layers']

    hfinal = []
    layers = []
    docker_history = []
    imageArch = ""

    # get "history"    
    try:
        with open(os.path.join(copydir, imageId+".tar"), 'r') as FH:
            configdata = json.loads(FH.read())
            rawhistory = configdata['history']
            imageArch = configdata['architecture']
    except Exception as err:
        raise err

    try:
        done=False
        idx = 0
        while not done:
            if not rawhistory:
                done = True
            else:
                hel = rawhistory.pop(0)
                if 'empty_layer' in hel and hel['empty_layer']:
                    lid = "<missing>"
                    lsize = 0
                else:
                    lel = rawlayers.pop(0)
                    lid = lel['digest']
                    layers.append(lid)
                    lsize = lel['size']

                try:
                    lcreatedby = hel['created_by']
                except:
                    lcreatedby = ""

                lcreated = hel['created']

                hfinal.append(
                    {
                        'Created': lcreated,
                        'CreatedBy': lcreatedby,
                        'Comment': '',
                        'Id': lid,
                        'Size': lsize,
                        'Tags': []
                    }
                )

        docker_history = hfinal
        if hfinal:
            with open(os.path.join(unpackdir, "docker_history.json"), 'w') as OFH:
                OFH.write(json.dumps(hfinal))
    except Exception as err:
        raise err

    if not dockerfile_contents:
        # get dockerfile_contents (translate history to guessed DF)
        # TODO 'FROM' guess?
        dockerfile_contents = "FROM scratch\n"
        for hel in docker_history:
            patt = re.match("^/bin/sh -c #\(nop\) +(.*)", hel['CreatedBy'])
            if patt:
                cmd = patt.group(1)
            else:
                cmd = "RUN " + hel['CreatedBy']
            dockerfile_contents = dockerfile_contents + cmd + "\n"        
        dockerfile_mode = "Guessed"
    elif not dockerfile_mode:
        dockerfile_mode = "Actual"

    return(docker_history, layers, dockerfile_contents, dockerfile_mode, imageArch)

def unpack(staging_dirs, layers):
    outputdir = staging_dirs['outputdir']
    unpackdir = staging_dirs['unpackdir']
    copydir = staging_dirs['copydir']

    try:
        squashtar, imageSize = squash(unpackdir, layers)
    except Exception as err:
        raise err
    return(imageSize)


def run_anchore_analyzers(staging_dirs, imageDigest, imageId):
    outputdir = staging_dirs['outputdir']
    unpackdir = staging_dirs['unpackdir']
    copydir = staging_dirs['copydir']

    # run analyzers
    anchore_module_root = resource_filename("anchore", "anchore-modules")
    analyzer_root = os.path.join(anchore_module_root, "analyzers")
    for f in os.listdir(analyzer_root):
        thecmd = os.path.join(analyzer_root, f)
        if re.match(".*\.py$", thecmd):
            cmdstr = " ".join([thecmd, imageId, unpackdir, outputdir, unpackdir])
            if True:
                try:
                    rc, sout, serr = anchore_engine.services.common.run_command(cmdstr)
                    if rc != 0:
                        raise Exception("command failed: cmd="+str(cmdstr)+" exitcode="+str(rc)+" stdout="+str(sout).strip()+" stderr="+str(serr).strip())
                    else:
                        logger.debug("command succeeded: cmd="+str(cmdstr)+" stdout="+str(sout).strip()+" stderr="+str(serr).strip())
                except Exception as err:
                    logger.error("command failed with exception - " + str(err))
                    #raise err

    analyzer_manifest = {}
    #TODO populate analyzer_manifest?
    analyzer_report = {}
    for analyzer_output in os.listdir(os.path.join(outputdir, "analyzer_output")):
        if analyzer_output not in analyzer_report:
            analyzer_report[analyzer_output] = {}

        for analyzer_output_el in os.listdir(os.path.join(outputdir, "analyzer_output", analyzer_output)):
            if analyzer_output_el not in analyzer_report[analyzer_output]:
                analyzer_report[analyzer_output][analyzer_output_el] = {'base': {}}

            data = read_kvfile_todict(os.path.join(outputdir, "analyzer_output", analyzer_output, analyzer_output_el))
            if data:
                analyzer_report[analyzer_output][analyzer_output_el]['base'] = read_kvfile_todict(os.path.join(outputdir, "analyzer_output", analyzer_output, analyzer_output_el))
            else:
                analyzer_report[analyzer_output].pop(analyzer_output_el, None)

        if not analyzer_report[analyzer_output]:
            analyzer_report.pop(analyzer_output, None)

    return(analyzer_report)

def generate_image_export(staging_dirs, imageDigest, imageId, analyzer_report, imageSize, fulltag, docker_history, dockerfile_mode, dockerfile_contents, layers, familytree, imageArch, rdigest, analyzer_manifest):
    image_report = []
    image_report.append(
        {
            'image': 
            {
                'imageId': imageId,
                'imagedata':
                {
                    'analyzer_manifest': analyzer_manifest,
                    'analysis_report': analyzer_report,
                    'image_report': {
                        'meta': {
                            'shortparentId': '', 
                            'sizebytes': imageSize, 
                            'imageId': imageId,
                            'usertype': None, 
                            'shortId': imageId[0:12], 
                            'imagename': imageId, 
                            'parentId': '', 
                            'shortname': imageId[0:12], 
                            'humanname': fulltag
                        },
                        'docker_history': docker_history,
                        'dockerfile_mode': dockerfile_mode,
                        'dockerfile_contents': dockerfile_contents,
                        'layers': layers,
                        'familytree': familytree,
                        'docker_data': {
                            'Architecture': imageArch,
                            'RepoDigests': [rdigest],
                            'RepoTags': [fulltag]
                        }
                    }
                }
            }
        }
    )
    return(image_report)
    
def analyze_image(userId, manifest, image_record, tmprootdir, registry_creds=[]):
    # need all this

    imageId = None
    imageDigest = None
    layers = []
    rawlayers = []
    familytree = []
    imageSize = 0
    analyzer_manifest = {}
    analyzer_report = {}
    imageArch = ""
    dockerfile_mode = ""
    docker_history = {}
    rdigest = ""
    staging_dirs = None

    try:
        imageDigest = image_record['imageDigest']
        try:
            manifest_data = json.loads(manifest)
        except Exception as err:
            raise Exception("cannot load manifest as JSON rawmanifest="+str(manifest)+") - exception: " + str(err))

        if image_record['dockerfile_mode']:
            dockerfile_mode = image_record['dockerfile_mode']

        image_detail = image_record['image_detail'][0]
        pullstring = image_detail['registry'] + "/" + image_detail['repo'] + "@" + image_detail['imageDigest']
        fulltag = image_detail['registry'] + "/" + image_detail['repo'] + ":" + image_detail['tag']
        imageId = image_detail['imageId']
        if image_detail['dockerfile']:
            dockerfile_contents = image_detail['dockerfile'].decode('base64')
        else:
            dockerfile_contents = None

        try:
            staging_dirs = make_staging_dirs(tmprootdir)
        except Exception as err:
            raise err

        try:
            rc = pull_image(staging_dirs, pullstring, registry_creds=registry_creds)
        except Exception as err:
            raise Exception("failed to pull image ("+str(pullstring)+") - exception: " + str(err))

        try:
            if manifest_data['schemaVersion'] == 1:
                docker_history, layers, dockerfile_contents, dockerfile_mode, imageArch = get_image_metadata_v1(staging_dirs, imageDigest, imageId, manifest_data, dockerfile_contents=dockerfile_contents, dockerfile_mode=dockerfile_mode)
            elif manifest_data['schemaVersion'] == 2:
                docker_history, layers, dockerfile_contents, dockerfile_mode, imageArch = get_image_metadata_v2(staging_dirs, imageDigest, imageId, manifest_data, dockerfile_contents=dockerfile_contents, dockerfile_mode=dockerfile_mode)
            else:
                raise Exception("unknown manifest schemaVersion")
            
        except Exception as err:
            raise Exception("failed to parse out manifest ("+str(pullstring)+") - exception: " + str(err))

        familytree = layers

        imageSize = unpack(staging_dirs, layers)
        familytree = layers
        analyzer_report = run_anchore_analyzers(staging_dirs, imageDigest, imageId)

        image_report = generate_image_export(staging_dirs, imageDigest, imageId, analyzer_report, imageSize, fulltag, docker_history, dockerfile_mode, dockerfile_contents, layers, familytree, imageArch, pullstring, analyzer_manifest)

    except Exception as err:
        raise Exception("failed to download, unpack, analyze, and generate image export - exception: " + str(err))
    finally:
        if staging_dirs:
            rc = delete_staging_dirs(staging_dirs)

    #if not imageDigest or not imageId or not manifest or not image_report:
    if not image_report:
        raise Exception("failed to analyze")

    return(image_report)
