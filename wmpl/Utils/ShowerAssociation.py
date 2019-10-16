""" Associate the given meteor trajectory to a meteor shower. """


from __future__ import print_function, division, absolute_import


import os
import copy

# Preserve Python 2 compatibility for the encoding option in the "open" function
from io import open

import numpy as np


from wmpl.Config import config
from wmpl.Utils.Math import angleBetweenSphericalCoords


class MeteorShower(object):
    def __init__(self, la_sun, L_g, B_g, v_g, IAU_no):
        """ Container for meteor shower parameters. """

        self.la_sun = la_sun
        self.L_g = L_g
        self.B_g = B_g
        self.v_g = v_g
        self.IAU_no = IAU_no


        # Find the shower code and name in the IAU table
        IAU_Shower_line = iau_shower_list[iau_shower_list[:, 1].astype(np.int) == self.IAU_no][0]
        self.IAU_code = IAU_Shower_line[3]
        self.IAU_name = IAU_Shower_line[4]


    def __repr__(self):

        out_str  = "Shower: {:d} {:s} {:s}\n".format(self.IAU_no, self.IAU_code, self.IAU_name)
        out_str += "    Sol: {:.6f} deg\n".format(np.degrees(self.la_sun))
        out_str += "    L_g: {:.6f} deg\n".format(np.degrees(self.L_g))
        out_str += "    B_g: {:.6f} deg\n".format(np.degrees(self.B_g))
        out_str += "    V_g: {:.3f} km/s\n".format(self.v_g/1000)


        return out_str


def loadJenniskensShowers(dir_path, file_name):
    """ Load the showers from the Jenniskens et al. (2018) table and init MeteorShower objects. """


    jenniskens_shower_list = []

    with open(os.path.join(dir_path, file_name), encoding='cp1252') as f:

        data_start = 0
        for line in f:

            # Find the beginning of the data table
            if "====================================" in line:
                data_start += 1
                continue


            # Skip all non-data lines
            if data_start < 2:
                continue


            line = line.replace('\n', '').replace('\r', '')

            # Skip empty lines
            if not line:
                continue


            # Stop if the final line was reached
            if "[FINAL]" in line:
                break

            # Unpack the shower data
            l0, L_l0, B_g, v_g, IAU_no = line.split()

            # Convert values to radians and m/s
            jenniskens_shower_list.append([np.radians(float(l0)), np.radians(float(L_l0)), 
                np.radians(float(B_g)), 1000*float(v_g), int(IAU_no)])



    return np.array(jenniskens_shower_list)



# Load the Jenniskens table on startup
jenniskens_shower_list = loadJenniskensShowers(*os.path.split(config.jenniskens_shower_table_file))

# Load the IAU table
iau_shower_list = np.loadtxt(config.iau_shower_table_file, delimiter="|", usecols=range(20), dtype=str)


def associateShowersJenniskens(la_sun, L_g, B_g, v_g, sol_window=1.0, max_radius=3.0, \
    max_veldif_percent=10.0):
    """ Given the shower radiant in Sun-centered ecliptic coordinates, associate it to a meteor shower
        using the showers listed in the Jenniskens et al. (2018) paper. 

    Arguments:
        la_sun: [float] Solar longitude (radians).
        L_g: [float] Sun-centered ecliptic longitude (i.e. geocentric ecliptic longitude minus the 
            solar longitude) (radians).
        B_g: [float] Sun-centered geocentric ecliptic latitude (radians).
        v_g: [float] Geocentric velocity (m/s).

    Keyword arguments:
        sol_window: [float] Solar longitude window of association (deg).
        max_radius: [float] Maximum angular separation from reference radiant (deg).
        max_veldif_percent: [float] Maximum velocity difference in percent.

    Return:
        [MeteorShower instance] MeteorShower instance for the closest match, or None for sporadics.
    """

    # Create a working copy of the Jenniskens shower list
    temp_shower_list = copy.deepcopy(jenniskens_shower_list)


    # Find all showers in the solar longitude window
    la_sun_diffs = np.abs((temp_shower_list[:, 0] - la_sun + np.pi)%(2*np.pi) - np.pi)
    temp_shower_list = temp_shower_list[la_sun_diffs <= np.radians(sol_window)]


    # Check if any associations were found
    if not len(temp_shower_list):
        return None

    
    # Find all showers within the maximum radiant distance radius
    radiant_distances = angleBetweenSphericalCoords(temp_shower_list[:, 2], temp_shower_list[:, 1], B_g, \
        (L_g - la_sun)%(2*np.pi))
    temp_shower_list = temp_shower_list[radiant_distances <= np.radians(max_radius)]



    # Check if any associations were found
    if not len(temp_shower_list):
        return None


    # Find all showers within the maximum velocity difference limit
    velocity_diff_percents = np.abs(100*(temp_shower_list[:, 3] - v_g)/temp_shower_list[:, 3])
    temp_shower_list = temp_shower_list[velocity_diff_percents <= max_veldif_percent]

    # Check if any associations were found
    if not len(temp_shower_list):
        return None


    ### Choose the best matching shower by the solar longitude, radiant, and velocity closeness ###

    # Compute the closeness parameters as a sum of normalized closeness by every individual parameter
    sol_dist_norm = np.abs(((temp_shower_list[:, 0] - la_sun + np.pi)%(2*np.pi) \
        - np.pi))/np.radians(sol_window)
    rad_dist_norm = angleBetweenSphericalCoords(temp_shower_list[:, 2], temp_shower_list[:, 1], B_g, (L_g \
        - la_sun)%(2*np.pi))/np.radians(max_radius)
    vg_dist_norm = np.abs(100*(temp_shower_list[:, 3] - v_g)/temp_shower_list[:, 3])/max_veldif_percent
    closeness_param = sol_dist_norm + rad_dist_norm + vg_dist_norm

    # Choose the best matching shower
    best_shower = temp_shower_list[np.argmin(closeness_param)]

    ### ###

    print(best_shower)

    # Init a shower object
    l0, L_l0, B_g, v_g, IAU_no = best_shower
    shower_obj = MeteorShower(l0, (L_l0 + l0)%360, B_g, v_g, int(round(IAU_no)))


    return shower_obj




if __name__ == "__main__":


    import time

    ### Test shower association ###


    # Draconid meteor
    la_sun = np.radians(195.402007)
    L_g = np.radians(247.41825)
    B_g = np.radians(78.95281)
    v_g = 1000*19.83965


    t1 = time.time()
    print(associateShowersJenniskens(la_sun, L_g, B_g, v_g))
    print("Time for association:", time.time() - t1)