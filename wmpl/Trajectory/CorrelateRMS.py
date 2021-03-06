""" Script which automatically pairs meteor observations from different RMS stations and computes
    trajectories. 
"""

from __future__ import print_function, division, absolute_import

import os
import sys
import re
import argparse
import json
import copy
import datetime
import shutil
import time

import numpy as np

from wmpl.Formats.CAMS import loadFTPDetectInfo
from wmpl.Trajectory.CorrelateEngine import TrajectoryCorrelator, TrajectoryConstraints
from wmpl.Utils.Math import generateDatetimeBins
from wmpl.Utils.Pickling import loadPickle, savePickle
from wmpl.Utils.TrajConversions import jd2Date



### CONSTANTS ###

# Name of the ouput trajectory directory
OUTPUT_TRAJ_DIR = "trajectories"

# Name of json file with the list of processed directories
JSON_DB_NAME = "processed_trajectories.json"

# Auto run frequency (hours)
AUTO_RUN_FREQUENCY = 6

### ###



class TrajectoryReduced(object):
    def __init__(self, traj_file_path, json_dict=None, traj_obj=None):
        """ Reduced representation of the Trajectory object which helps to save memory. 

        Arguments:
            traj_file_path: [str] Full path to the trajectory object.

        Keyword arguments:
            json_dict: [dict] Load values from a dictionary instead of a traj pickle file. None by default,
                in which case a traj pickle file will be loaded.
            traj_obj: [Trajectory instance] Load values from a full Trajectory object, instead from the disk.
                None by default.
        """

        # Load values from a JSON pickle file
        if json_dict is None:

            if traj_obj is None:

                # Full path to the trajectory object (saved to it can be loaded later if needed)
                self.traj_file_path = traj_file_path

                # Load the trajectory object
                traj = loadPickle(*os.path.split(traj_file_path))

            else:

                # Load values from a given trajectory file
                traj = traj_obj
                self.traj_file_path = os.path.join(traj.output_dir, traj.file_name + "_trajectory.pickle")


            # Reference Julian date (beginning of the meteor)
            self.jdt_ref = traj.jdt_ref

            # ECI coordinates of the state vector computed through minimization
            if traj.state_vect_mini is None:
                self.state_vect_mini = None
            else:
                self.state_vect_mini = traj.state_vect_mini.tolist()

            # Apparent radiant vector computed through minimization
            if traj.radiant_eci_mini is None:
                self.radiant_eci_mini = None
            else:
                self.radiant_eci_mini = traj.radiant_eci_mini.tolist()

            # Initial and average velocity
            self.v_init = traj.v_init
            self.v_avg = traj.v_avg

            # Coordinates of the first point (observed)
            self.rbeg_lat = traj.rbeg_lat
            self.rbeg_lon = traj.rbeg_lon
            self.rbeg_ele = traj.rbeg_ele
            self.rbeg_jd = traj.rbeg_jd

            # Coordinates of the last point (observed)
            self.rend_lat = traj.rend_lat
            self.rend_lon = traj.rend_lon
            self.rend_ele = traj.rend_ele
            self.rend_jd = traj.rend_jd

            # Stations participating in the solution
            self.participating_stations = sorted([obs.station_id for obs in traj.observations \
                if obs.ignore_station == False])

            # Ignored stations
            self.ignored_stations = sorted([obs.station_id for obs in traj.observations \
                if obs.ignore_station == True])


        # Load values from a dictionary
        else:
            self.__dict__ = json_dict



class DatabaseJSON(object):
    def __init__(self, db_file_path):

        self.db_file_path = db_file_path

        # List of processed directories (keys are station codes, values are relative paths to night 
        #   directories)
        self.processed_dirs = {}

        # List of paired observations as a part of a trajectory (keys are station codes, values are unique 
        #   observation IDs)
        self.paired_obs = {}

        # List of processed trajectories (keys are trajectory reference julian dates, values are \
        #   TrajectoryReduced objects)
        self.trajectories = {}

        # List of failed trajectories (keys are trajectory reference julian dates, values are \
        #   TrajectoryReduced objects)
        self.failed_trajectories = {}

        # Load the database from a JSON file
        self.load()


    def load(self):
        """ Load the database from a JSON file. """

        if os.path.exists(self.db_file_path):
            with open(self.db_file_path) as f:
                self.__dict__ = json.load(f)

                # Convert trajectories from JSON to TrajectoryReduced objects
                for traj_dict_str in ["trajectories", "failed_trajectories"]:
                    traj_dict = getattr(self, traj_dict_str)
                    trajectories_obj_dict = {}
                    for traj_json in traj_dict:
                        traj_reduced_tmp = TrajectoryReduced(None, json_dict=traj_dict[traj_json])

                        trajectories_obj_dict[traj_reduced_tmp.jdt_ref] = traj_reduced_tmp

                    # Set the trajectory dictionary
                    setattr(self, traj_dict_str, trajectories_obj_dict)


    def save(self):
        """ Save the database of processed meteors to disk. """

        with open(self.db_file_path, 'w') as f:
            self2 = copy.deepcopy(self)

            # Convert reduced trajectory objects to JSON objects
            self2.trajectories = {key: self.trajectories[key].__dict__ for key in self.trajectories}
            self2.failed_trajectories = {key: self.failed_trajectories[key].__dict__ \
                for key in self.failed_trajectories}

            f.write(json.dumps(self2, default=lambda o: o.__dict__, indent=4, sort_keys=True))


    def addProcessedDir(self, station_name, rel_proc_path):
        """ Add the processed directory to the list. """

        if station_name in self.processed_dirs:
            if not rel_proc_path in self.processed_dirs[station_name]:
                self.processed_dirs[station_name].append(rel_proc_path)


    def addPairedObservation(self, met_obs):
        """ Mark the given meteor observation as paired in a trajectory. """

        if met_obs.station_code not in self.paired_obs:
            self.paired_obs[met_obs.station_code] = []

        if met_obs.id not in self.paired_obs[met_obs.station_code]:
            self.paired_obs[met_obs.station_code].append(met_obs.id)


    def checkObsIfPaired(self, met_obs):
        """ Check if the given observation has been paired to a trajectory or not. """

        if met_obs.station_code in self.paired_obs:
            return (met_obs.id in self.paired_obs[met_obs.station_code])

        else:
            return False


    def checkTrajIfFailed(self, traj):
        """ Check if the given trajectory has been computed with the same observations and has failed to be
            computed before.

        """

        # Check if the reference time is in the list of failed trajectories
        if traj.jdt_ref in self.failed_trajectories:

            # Get the failed trajectory object
            failed_traj = self.failed_trajectories[traj.jdt_ref]

            # Check if the same observations participate in the failed trajectory as in the trajectory that
            #   is being tested
            all_match = True
            for obs in traj.observations:
                
                if not ((obs.station_id in failed_traj.participating_stations) \
                    or (obs.station_id in failed_traj.ignored_stations)):

                    all_match = False
                    break

            # If the same stations were used, the trajectory estimation failed before
            if all_match:
                return True


        return False


    def addTrajectory(self, traj_file_path, traj_obj=None, failed=False):
        """ Add a computed trajectory to the list. 
    
        Arguments:
            traj_file_path: [str] Full path the trajectory object.

        Keyword arguments:
            traj_obj: [bool] Instead of loading a traj object from disk, use the given object.
            failed: [bool] Add as a failed trajectory. False by default.
        """

        # Load the trajectory from disk
        if traj_obj is None:

            # Init the reduced trajectory object
            traj_reduced = TrajectoryReduced(traj_file_path)

        else:

            # Use the provided trajectory object
            traj_reduced = traj_obj


        # Choose to which dictionary the trajectory will be added
        if failed:
            traj_dict = self.failed_trajectories

        else:
            traj_dict = self.trajectories


        # Add the trajectory to the list (key is the reference JD)
        if traj_reduced.jdt_ref not in traj_dict:
            traj_dict[traj_reduced.jdt_ref] = traj_reduced



    def removeTrajectory(self, traj_reduced):
        """ Remove the trajectory from the data base and disk. """

        # Remove the trajectory data base entry
        if traj_reduced.jdt_ref in self.trajectories:
            del self.trajectories[traj_reduced.jdt_ref]

        # Remove the trajectory folder on the disk
        if os.path.isfile(traj_reduced.traj_file_path):

            traj_dir = os.path.dirname(traj_reduced.traj_file_path)
            shutil.rmtree(traj_dir)






class MeteorPointRMS(object):
    def __init__(self, frame, time_rel, x, y, ra, dec, azim, alt, mag):
        """ Container for individual meteor picks. """

        # Frame number since the beginning of the FF file
        self.frame = frame
        
        # Relative time
        self.time_rel = time_rel

        # Image coordinats
        self.x = x
        self.y = y
        
        # Equatorial coordinates (J2000, deg)
        self.ra = ra
        self.dec = dec

        # Horizontal coordinates (J2000, deg), azim is +E of due N
        self.azim = azim
        self.alt = alt

        self.intensity_sum = None

        self.mag = mag



class MeteorObsRMS(object):
    def __init__(self, station_code, reference_dt, platepar, data, rel_proc_path):
        """ Container for meteor observations with the interface compatible with the trajectory correlator
            interface. 
        """

        self.station_code = station_code

        self.reference_dt = reference_dt
        self.platepar = platepar
        self.data = data

        # Path to the directory with data
        self.rel_proc_path = rel_proc_path

        # Internal flags to control the processing flow
        # NOTE: The processed flag should always be set to False for every observation when the program starts
        self.processed = False 

        # Mean datetime of the observation
        self.mean_dt = self.reference_dt + datetime.timedelta(seconds=np.mean([entry.time_rel \
            for entry in self.data]))

        
        ### Estimate if the meteor begins and ends inside the FOV ###

        self.fov_beg = False
        self.fov_end = False

        half_index = len(data)//2


        # Find angular velocity at the beginning per every axis
        dxdf_beg = (self.data[half_index].x - self.data[0].x)/(self.data[half_index].frame \
            - self.data[0].frame)
        dydf_beg = (self.data[half_index].y - self.data[0].y)/(self.data[half_index].frame \
            - self.data[0].frame)

        # Compute locations of centroids 2 frames before the beginning
        x_pre_begin = self.data[0].x - 2*dxdf_beg
        y_pre_begin = self.data[0].y - 2*dydf_beg

        # If the predicted point is inside the FOV, mark it as such
        if (x_pre_begin > 0) and (x_pre_begin <= self.platepar.X_res) and (y_pre_begin > 0) \
            and (y_pre_begin < self.platepar.Y_res):

            self.fov_beg = True

        # If the starting point is not inside the FOV, exlude the first point
        else:
            self.data = self.data[1:]


        # Find angular velocity at the ending per every axis
        dxdf_end = (self.data[-1].x - self.data[half_index].x)/(self.data[-1].frame \
            - self.data[half_index].frame)
        dydf_end = (self.data[-1].y - self.data[half_index].y)/(self.data[-1].frame \
            - self.data[half_index].frame)

        # Compute locations of centroids 2 frames after the end
        x_post_end = self.data[-1].x + 2*dxdf_end
        y_post_end = self.data[-1].y + 2*dydf_end

        # If the predicted point is inside the FOV, mark it as such
        if (x_post_end > 0) and (x_post_end <= self.platepar.X_res) and (y_post_end > 0) \
            and (y_post_end <= self.platepar.Y_res):
            
            self.fov_end = True

        # If the ending point is not inside fully inside the FOV, exclude it
        else:
            self.data = self.data[:-1]

        ### ###


        # Generate a unique observation ID, the format is: STATIONID_YYYYMMDD-HHMMSS.us_CHECKSUM
        #  where CHECKSUM is the last four digits of the sum of all observation image X cordinates
        checksum = int(np.sum([entry.x for entry in self.data])%10000)
        self.id = "{:s}_{:s}_{:04d}".format(self.station_code, self.mean_dt.strftime("%Y%m%d-%H%M%S.%f"), \
            checksum)



class PlateparDummy:
    def __init__(self, **entries):
        """ This class takes a platepar dictionary and converts it into an object. """

        self.__dict__.update(entries)



class RMSDataHandle(object):
    def __init__(self, dir_path):
        """ Handles data interfacing between the trajectory correlator and RMS data files on disk. 
    
        Arguments:
            dir_path: [str] Path to the directory with data files. 
        """

        self.dir_path = dir_path

        print("Using directory:", self.dir_path)

        # Load the list of stations
        station_list = self.loadStations()

        # Load database of processed folders
        self.db = DatabaseJSON(os.path.join(self.dir_path, JSON_DB_NAME))

        # Find unprocessed meteor files
        self.processing_list = self.findUnprocessedFolders(station_list)

        # Load already computed trajectories
        self.loadComputedTrajectories(os.path.join(self.dir_path, OUTPUT_TRAJ_DIR))


    def loadStations(self):
        """ Load the station names in the processing folder. """

        station_list = []

        for dir_name in os.listdir(self.dir_path):

            # Check if the dir name matches the station name pattern
            if os.path.isdir(os.path.join(self.dir_path, dir_name)):
                if re.match("^[A-Z]{2}[A-Z0-9]{4}$", dir_name):
                    print("Using station:", dir_name)
                    station_list.append(dir_name)
                else:
                    print("Skipping directory:", dir_name)


        return station_list



    def findUnprocessedFolders(self, station_list):
        """ Go through directories and find folders with unprocessed data. """

        processing_list = []

        # skipped_dirs = 0

        # Go through all station directories
        for station_name in station_list:

            station_path = os.path.join(self.dir_path, station_name)

            # Add the station name to the database if it doesn't exist
            if station_name not in self.db.processed_dirs:
                self.db.processed_dirs[station_name] = []

            # Go through all directories in stations
            for night_name in os.listdir(station_path):

                night_path = os.path.join(station_path, night_name)
                night_path_rel = os.path.join(station_name, night_name)

                # Extract the date and time of directory, if possible
                try:
                    night_dt = datetime.datetime.strptime("_".join(night_name.split("_")[1:3]), \
                        "%Y%m%d_%H%M%S")
                except:
                    print("Could not parse the date of the night dir: {:s}".format(night_path))
                    night_dt = None

                # # If the night path is not in the processed list, add it to the processing list
                # if night_path_rel not in self.db.processed_dirs[station_name]:
                #     processing_list.append([station_name, night_path_rel, night_path, night_dt])

                processing_list.append([station_name, night_path_rel, night_path, night_dt])

                # else:
                #     skipped_dirs += 1


        # if skipped_dirs:
        #     print("Skipped {:d} processed directories".format(skipped_dirs))

        return processing_list



    def initMeteorObs(self, station_code, ftpdetectinfo_path, platepars_recalibrated_dict):
        """ Init meteor observations from the FTPdetectinfo file and recalibrated platepars. """

        # Load station coordinates
        if len(list(platepars_recalibrated_dict.keys())):
            
            pp_dict = platepars_recalibrated_dict[list(platepars_recalibrated_dict.keys())[0]]
            pp = PlateparDummy(**pp_dict)
            stations_dict = {station_code: [np.radians(pp.lat), np.radians(pp.lon), pp.elev]}

            # Load the FTPdetectinfo file
            meteor_list = loadFTPDetectInfo(ftpdetectinfo_path, stations_dict)

        else:
            meteor_list = []


        return meteor_list



    def loadUnpairedObservations(self, processing_list, dt_range=None):
        """ Load unpaired meteor observations, i.e. observations that are not a part of any trajectory. """

        # Go through folders for processing
        unpaired_met_obs_list = []
        for station_code, rel_proc_path, proc_path, night_dt in processing_list:

            # Check that the night datetime is within the given range of times, if the range is given
            if (dt_range is not None) and (night_dt is not None):
                dt_beg, dt_end = dt_range

                # Skip all folders which are outside the limits
                if (night_dt < dt_beg) or (night_dt > dt_end):
                    continue



            ftpdetectinfo_name = None
            platepar_recalibrated_name = None

            # Find FTPdetectinfo and platepar files
            for name in os.listdir(proc_path):
                    
                # Find FTPdetectinfo
                if name.startswith("FTPdetectinfo") and name.endswith('.txt') and \
                    (not "backup" in name) and (not "uncalibrated" in name):
                    ftpdetectinfo_name = name
                    continue

                if name == "platepars_all_recalibrated.json":
                    platepar_recalibrated_name = name
                    continue

            # Skip these observations if no data files were found inside
            if (ftpdetectinfo_name is None) or (platepar_recalibrated_name is None):
                print("Skipping {:s} due to missing data files...".format(rel_proc_path))

                # Add the folder to the list of processed folders
                self.db.addProcessedDir(station_code, rel_proc_path)

                continue

            # Save database to mark those with missing data files
            self.db.save()


            # Load platepars
            with open(os.path.join(proc_path, platepar_recalibrated_name)) as f:
                platepars_recalibrated_dict = json.load(f)

            # If all files exist, init the meteor container object
            cams_met_obs_list = self.initMeteorObs(station_code, os.path.join(proc_path, \
                ftpdetectinfo_name), platepars_recalibrated_dict)

            # Format the observation object to the one required by the trajectory correlator
            for cams_met_obs in cams_met_obs_list:

                # Get the platepar
                if cams_met_obs.ff_name in platepars_recalibrated_dict:
                    pp_dict = platepars_recalibrated_dict[cams_met_obs.ff_name]
                else:
                    continue

                pp = PlateparDummy(**pp_dict)

                # Init meteor data
                meteor_data = []
                for entry in zip(cams_met_obs.frames, cams_met_obs.time_data, cams_met_obs.x_data,\
                    cams_met_obs.y_data, cams_met_obs.azim_data, cams_met_obs.elev_data, \
                    cams_met_obs.ra_data, cams_met_obs.dec_data, cams_met_obs.mag_data):

                    frame, time_rel, x, y, azim, alt, ra, dec, mag = entry

                    met_point = MeteorPointRMS(frame, time_rel, x, y, np.degrees(ra), np.degrees(dec), \
                        np.degrees(azim), np.degrees(alt), mag)

                    meteor_data.append(met_point)


                # Init the new meteor observation object
                met_obs = MeteorObsRMS(station_code, jd2Date(cams_met_obs.jdt_ref, dt_obj=True), pp, \
                    meteor_data, rel_proc_path)

                # Add only unpaired observations
                if not self.db.checkObsIfPaired(met_obs):

                    print(station_code, met_obs.reference_dt, rel_proc_path)

                    unpaired_met_obs_list.append(met_obs)


        return unpaired_met_obs_list



    def loadComputedTrajectories(self, traj_dir_path):
        """ Load all already estimated trajectories. 

        Arguments:
            traj_dir_path: [str] Full path to a directory with trajectory pickles.
        """

        # Find and load all trajectory objects
        for entry in sorted(os.walk(traj_dir_path), key=lambda x: x[0]):

            dir_path, _, file_names = entry

            # Find and load all trajectory pickle files
            for file_name in file_names:
                if file_name.endswith("_trajectory.pickle"):
                    self.db.addTrajectory(os.path.join(dir_path, file_name))


    def getComputedTrajectories(self, jd_beg, jd_end):
        """ Returns a list of computed trajectories between the Julian dates.
        """

        return [self.db.trajectories[key] for key in self.db.trajectories \
            if (self.db.trajectories[key].jdt_ref >= jd_beg) \
                and (self.db.trajectories[key].jdt_ref <= jd_end)]
                


    def getPlatepar(self, met_obs):
        """ Return the platepar of the meteor observation. """

        return met_obs.platepar



    def getUnpairedObservations(self):
        """ Returns a list of unpaired meteor observations. """

        return self.unpaired_observations


    def findTimePairs(self, met_obs, unpaired_observations, max_toffset):
        """ Finds pairs in time between the given meteor observations and all other observations from 
            different stations. 

        Arguments:
            met_obs: [MeteorObsRMS] Object containing a meteor observation.
            unpaired_observations: [list] A list of MeteorObsRMS objects which will be paired in time with
                the given object.
            max_toffset: [float] Maximum offset in time (seconds) for pairing.

        Return:
            [list] A list of MeteorObsRMS instances with are offten in time less than max_toffset from 
                met_obs.
        """

        found_pairs = []

        # Go through all meteors from other stations
        for met_obs2 in unpaired_observations:

            # Take only observations from different stations
            if met_obs.station_code == met_obs2.station_code:
                continue

            # Take observations which are within the given time window
            if abs((met_obs.mean_dt - met_obs2.mean_dt).total_seconds()) <= max_toffset:
                found_pairs.append(met_obs2)


        return found_pairs


    def getTrajTimePairs(self, traj_reduced, unpaired_observations, max_toffset):
        """ Find unpaired observations which are close in time to the given trajectory. """

        found_traj_obs_pairs = []

        # Compute the middle time of the trajectory as reference time
        traj_mid_dt = jd2Date((traj_reduced.rbeg_jd + traj_reduced.rend_jd)/2, dt_obj=True)

        # Go through all unpaired observations
        for met_obs in unpaired_observations:

            # Skip all stations that are already participating in the trajectory solution
            if (met_obs.station_code in traj_reduced.participating_stations) or \
                (met_obs.station_code in traj_reduced.ignored_stations):

                continue


            # Take observations which are within the given time window from the trajectory
            if abs((met_obs.mean_dt - traj_mid_dt).total_seconds()) <= max_toffset:
                found_traj_obs_pairs.append(met_obs)


        return found_traj_obs_pairs


    def generateTrajOutputDirectoryPath(self, traj):
        """ Generate a path to the trajectory output directory. """

        # Generate a list of station codes
        if isinstance(traj, TrajectoryReduced):
            # If the reducted trajectory object is given
            station_list = traj.participating_stations

        else:
            # If the full trajectory object is given
            station_list = [obs.station_id for obs in traj.observations if obs.ignore_station is False]

        return os.path.join(self.dir_path, OUTPUT_TRAJ_DIR, \
            jd2Date(traj.jdt_ref, dt_obj=True).strftime("%Y%m%d_%H%M%S.%f")[:-3] + "_" \
            + "_".join(list(set([stat_id[:2] for stat_id in station_list]))))


    def saveTrajectoryResults(self, traj, save_plots):
        """ Save trajectory results to the disk. """


        # Generate the name for the output directory (add list of country codes at the end)
        output_dir = self.generateTrajOutputDirectoryPath(traj)

        # Save the report
        traj.saveReport(output_dir, traj.file_name + '_report.txt', uncertainties=traj.uncertainties, 
            verbose=False)

        # Save the picked trajectory structure
        savePickle(traj, output_dir, traj.file_name + '_trajectory.pickle')

        # Save the plots
        if save_plots:
            traj.save_results = True
            traj.savePlots(output_dir, traj.file_name, show_plots=False)
            traj.save_results = False



    def markObservationAsProcessed(self, met_obs):
        """ Mark the given meteor observation as processed. """

        self.db.addProcessedDir(met_obs.station_code, met_obs.rel_proc_path)



    def markObservationAsPaired(self, met_obs):
        """ Mark the given meteor observation as paired in a trajectory. """

        self.db.addPairedObservation(met_obs)



    def addTrajectory(self, traj, failed_jdt_ref=None):
        """ Add the resulting trajectory to the database. 

        Arguments:
            traj: [Trajectory object]
            failed_jdt_ref: [float] Reference Julian date of the failed trajectory. None by default.
        """

        # Set the correct output path
        traj.output_dir = self.generateTrajOutputDirectoryPath(traj)

        # Convert the full trajectory object into the reduced trajectory object
        traj_reduced = TrajectoryReduced(None, traj_obj=traj)

        # If the trajectory failed, keep track of the original reference Julian date, as it might have been
        #   changed during trajectory estimation
        if failed_jdt_ref is not None:
            traj_reduced.jdt_ref = failed_jdt_ref

        self.db.addTrajectory(None, traj_obj=traj_reduced, failed=(failed_jdt_ref is not None))



    def removeTrajectory(self, traj_reduced):
        """ Remove the trajectory from the data base and disk. """

        self.db.removeTrajectory(traj_reduced)



    def checkTrajIfFailed(self, traj):
        """ Check if the given trajectory has been computed with the same observations and has failed to be
            computed before.

        """

        return self.db.checkTrajIfFailed(traj)



    def loadFullTraj(self, traj_reduced):
        """ Load the full trajectory object. 
    
        Arguments:
            traj_reduced: [TrajectoryReduced object]

        Return:
            traj: [Trajectory object] or [None] if file not found
        """

        # Generate the path to the output directory
        output_dir = self.generateTrajOutputDirectoryPath(traj_reduced)

        # Get the file name
        file_name = os.path.basename(traj_reduced.traj_file_path)

        # Try loading a full trajectory
        try:
            traj = loadPickle(output_dir, file_name)

            return traj

        except FileNotFoundError:
            print("File {:s} not found!".format(traj_reduced.traj_file_path))
            
            return None



    def finish(self):
        """ Finish the processing run. """

        # Save the processed directories to the DB file
        self.db.save()

        # Save the list of processed meteor observations

        



if __name__ == "__main__":

    # Set matplotlib for headless running
    import matplotlib
    matplotlib.use('Agg')


    ### COMMAND LINE ARGUMENTS

    # Init the command line arguments parser
    arg_parser = argparse.ArgumentParser(description="""Automatically compute trajectories from RMS data in the given directory. 
The directory structure needs to be the following, for example:
    ./ # root directory
        /HR0001/
            /HR0001_20190707_192835_241084_detected
                ./FTPdetectinfo_HR0001_20190707_192835_241084.txt
                ./platepars_all_recalibrated.json
        /HR0004/
            ./FTPdetectinfo_HR0004_20190707_193044_498581.txt
            ./platepars_all_recalibrated.json
        /...

In essence, the root directory should contain directories of stations (station codes need to be exact), and these directories should
contain data folders. Data folders should have FTPdetectinfo files together with platepar files.""",
        formatter_class=argparse.RawTextHelpFormatter)

    arg_parser.add_argument('dir_path', type=str, help='Path to the root data directory. Trajectory helper files will be stored here as well.')

    arg_parser.add_argument('-t', '--maxtoffset', metavar='MAX_TOFFSET', \
        help='Maximum time offset between the stations. Default is 5 seconds.', type=float, default=10.0)

    arg_parser.add_argument('-s', '--maxstationdist', metavar='MAX_STATION_DIST', \
        help='Maximum distance (km) between stations of paired meteors. Default is 600 km.', type=float, \
        default=600.0)

    arg_parser.add_argument('-m', '--minerr', metavar='MIN_ARCSEC_ERR', \
        help="Minimum error in arc seconds below which the station won't be rejected. 30 arcsec by default.", \
        type=float)

    arg_parser.add_argument('-M', '--maxerr', metavar='MAX_ARCSEC_ERR', \
        help="Maximum error in arc seconds, above which the station will be rejected. 180 arcsec by default.", \
        type=float)

    arg_parser.add_argument('-v', '--maxveldiff', metavar='MAX_VEL_DIFF', \
        help='Maximum difference in percent between velocities between two stations. Default is 25 percent.', \
        type=float, default=25.0)

    arg_parser.add_argument('-p', '--velpart', metavar='VELOCITY_PART', \
        help='Fixed part from the beginning of the meteor on which the initial velocity estimation using the sliding fit will start. Default is 0.4 (40 percent), but for noisier data this should be bumped up to 0.5.', \
        type=float, default=0.40)

    arg_parser.add_argument('-d', '--disablemc', \
        help='Disable Monte Carlo.', action="store_true")

    arg_parser.add_argument('-u', '--uncerttime', \
        help="Compute uncertainties by culling solutions with worse value of the time fit than the LoS solution. This may increase the computation time considerably.", \
        action="store_true")

    arg_parser.add_argument('-l', '--saveplots', \
        help='Save plots to disk.', action="store_true")

    arg_parser.add_argument('-r', '--timerange', metavar='TIME_RANGE', \
        help="""Only compute the trajectories in the given range of time. The time range should be given in the format: "(YYYYMMDD-HHMMSS,YYYYMMDD-HHMMSS)".""", \
            type=str)

    arg_parser.add_argument('-a', '--auto', metavar='PREV_DAYS', type=float, default=None, const=14.0, \
        nargs='?', \
        help="""Run continously taking the data in the last PREV_DAYS to compute the new trajectories and update the old ones. The default time range is 14 days."""
        )


    # Parse the command line arguments
    cml_args = arg_parser.parse_args()

    ############################

    if cml_args.auto is None:
        print("Running trajectory estimation once!")
    else:
        print("Auto running trajectory estimation every {:.1f} hours using the last {:.1f} days of data...".format(AUTO_RUN_FREQUENCY, cml_args.auto))


    # Init trajectory constraints
    trajectory_constraints = TrajectoryConstraints()
    trajectory_constraints.max_toffset = cml_args.maxtoffset
    trajectory_constraints.max_station_dist = cml_args.maxstationdist
    trajectory_constraints.max_vel_percent_diff = cml_args.maxveldiff
    trajectory_constraints.run_mc = not cml_args.disablemc
    trajectory_constraints.save_plots = cml_args.saveplots
    trajectory_constraints.geometric_uncert = not cml_args.uncerttime

    if cml_args.minerr is not None:
        trajectory_constraints.min_arcsec_err = cml_args.minerr

    if cml_args.maxerr is not None:
        trajectory_constraints.max_arcsec_err = cml_args.maxerr


    # Run processing. If the auto run more is not on, the loop will break after one run
    previous_start_time = None
    while True: 

        # Clock for measuring script time
        t1 = datetime.datetime.utcnow()

        # Init the data handle
        dh = RMSDataHandle(cml_args.dir_path)


        # If there is nothing to process, stop
        if not dh.processing_list:
            print()
            print("Nothing to process!")
            print("Probably everything is already processed.")
            print("Exiting...")
            sys.exit()



        # If auto run is enabled, compute the time range to use
        event_time_range = None
        if cml_args.auto is not None:

            # Compute first date and time to use for auto run
            dt_beg = datetime.datetime.now() - datetime.timedelta(days=cml_args.auto)

            # If the beginning time is later than the beginning of the previous run, use the beginning of the
            # previous run minus two days as the beginning time
            if previous_start_time is not None:
                if dt_beg > previous_start_time:
                    dt_beg = previous_start_time - datetime.timedelta(days=2)


            # Use now as the upper time limit
            dt_end = datetime.datetime.now()

            event_time_range = [dt_beg, dt_end]


        # Otherwise check if the time range is given
        else:

            # If the time range to use is given, use it
            if cml_args.timerange is not None:

                # Extract time range
                time_beg, time_end = cml_args.timerange.strip("(").strip(")").split(",")
                dt_beg = datetime.datetime.strptime(time_beg, "%Y%m%d-%H%M%S")
                dt_end = datetime.datetime.strptime(time_end, "%Y%m%d-%H%M%S")

                print("Custom time range:")
                print("    BEG: {:s}".format(str(dt_beg)))
                print("    END: {:s}".format(str(dt_end)))

                event_time_range = [dt_beg, dt_end]


        ### GENERATE MONTHLY TIME BINS ###
        
        # Find the range of datetimes of all folders (take only those after the year 2000)
        proc_dir_dts = [entry[3] for entry in dh.processing_list if entry[3] is not None]
        proc_dir_dts = [dt for dt in proc_dir_dts if dt > datetime.datetime(2000, 1, 1, 0, 0, 0)]

        # Reject all folders not within the time range of interest +/- 1 day
        if event_time_range is not None:

            dt_beg, dt_end = event_time_range

            proc_dir_dts = [dt for dt in proc_dir_dts \
                if (dt >= dt_beg - datetime.timedelta(days=1)) and \
                    (dt <= dt_end + datetime.timedelta(days=1))]


        # Determine the limits of data
        proc_dir_dt_beg = min(proc_dir_dts)
        proc_dir_dt_end = max(proc_dir_dts)

        # Split the processing into monthly chunks
        dt_bins = generateDatetimeBins(proc_dir_dt_beg, proc_dir_dt_end, bin_days=30)

        print()
        print("ALL TIME BINS:")
        print("----------")
        for bin_beg, bin_end in dt_bins:
            print("{:s}, {:s}".format(str(bin_beg), str(bin_end)))


        ### ###


        # Go through all chunks in time
        for bin_beg, bin_end in dt_bins:

            print()
            print("PROCESSING TIME BIN:")
            print(bin_beg, bin_end)
            print("-----------------------------")
            print()

            # Load data of unprocessed observations
            dh.unpaired_observations = dh.loadUnpairedObservations(dh.processing_list, \
                dt_range=(bin_beg, bin_end))

            # Run the trajectory correlator
            tc = TrajectoryCorrelator(dh, trajectory_constraints, cml_args.velpart, data_in_j2000=True)
            tc.run(event_time_range=event_time_range)


        
        print("Total run time: {:s}".format(str(datetime.datetime.utcnow() - t1)))

        # Store the previous start time
        previous_start_time = copy.deepcopy(t1)

        # Break after one loop if auto mode is not on
        if cml_args.auto is None:
            break

        else:

            # Otherwise wait to run AUTO_RUN_FREQUENCY hours after the beginning
            wait_time = (datetime.timedelta(hours=AUTO_RUN_FREQUENCY) \
                - (datetime.datetime.utcnow() - t1)).total_seconds()

            # Run immediately if the wait time has elapsed
            if wait_time < 0:
                continue

            # Otherwise wait to run
            else:

                # Compute next run time
                next_run_time = datetime.datetime.now() + datetime.timedelta(seconds=wait_time)

                # Wait to run
                while next_run_time > datetime.datetime.now():
                    print("Waiting {:s} to run the trajectory solver...          ".format(str(next_run_time \
                        - datetime.datetime.now())), end='\r')
                    time.sleep(2)