import os
import sys
import glob
import netCDF4
import os.path
import datetime
import multiprocessing

import numpy as np
import scipy
import scipy.interpolate


def makedate(days):
    """Get a datetime object from the number of days past 1942-12-01."""

    date = datetime.date(1949, 12, 1) + datetime.timedelta(days)
    return datetime.date(date.year, date.month, 1)


def makemonth(days):
    """Get the month from the number of days past 1942-12-01."""

    return makedate(days).month


def makeyear(days):
    """Get the year from the number of days past 1942-12-01."""

    return makedate(days).year


def modelname(path):
    """Extract a model name from a specially formatted filename."""

    return '_'.join(path.split('_')[2:6:3])


def interpolate_variable_worker(args):
    """
    Process for parallel interpolation of several NetCDF files at once. Takes
    in a dictionary with arguments for interpolation.

    :param args (dict): Dictionary containing parameters for interpolation.
        Contains the following keys:
        id (int): Unique identifier for the process. Just for debug output.
        netcdf (str): path to NetCDF file.
        locations (np.ndarray): (n, 2) latitude, longitude pairs at which to
            interpolate.
        csv (str): CSV output path.
    """

    cpu_id = os.getpid()

    print '\t\tPID %d / File %d: opening %s.' % (cpu_id, args['id'], args['netcdf'])

    try:
        ncf = netCDF4.Dataset(args['netcdf'], 'r', format='NETCDF4')
        var = ncf.variables[args['var']][:]
        lats = ncf.variables['lat'][:]
        lons = ncf.variables['lon'][:]
        times = ncf.variables['time'][:]
    except:
        print 'Error loading NetCDF:', args
        raise

    try:
        # Correct for CMIP5 0 to 360. Make -180 to 180.
        lons[lons >= 180] = -(360 - lons[lons >= 180])

        # The 44i vs 44 CORDEX data is formatted a little differently, as lat
        # and lon aren't functions computed at each grid point, but are
        # dimensions, so you just have lat = (nlat,) and lon = (nlon,) rather
        # than each being repeated (nlat, nlon) matrices.
        if lats.ndim < 2:
            lats, lons = np.meshgrid(lats, lons)

            if var.ndim > 3:
                var = var.reshape((len(times), lats.shape[0], lats.shape[1]))
    except:
        print 'Error reshaping:', args, lats.shape, lons.shape, times.shape
        raise

    try:
        # Crop out bounding box.
        bb = np.array([
            [uniq[np.argmax(uniq >= lmin) - 1], uniq[np.argmax(uniq > lmax)]]
            for uniq, lmin, lmax in [
                (
                    sorted(np.unique(coords.flatten())),
                    np.amin(args['locations'][:, i]),
                    np.amax(args['locations'][:, i])
                )
                for i, coords in enumerate([lats, lons])
            ]
        ])

        latmin, latmax, lonmin, lonmax = bb[0, 0], bb[0, 1], bb[1, 0], bb[1, 1]

        valid_locs = np.bitwise_and(
            np.bitwise_and(lats <= latmax, lats >= latmin),
            np.bitwise_and(lons <= lonmax, lons >= lonmin)
        ).flatten()

        latlons = np.array([lats.flatten(), lons.flatten()]).T

        var = var.reshape((len(times), len(latlons)))[:, valid_locs]
        latlons = latlons[valid_locs]
    except:
        print 'Error cropping:', args, lats.shape, lons.shape, times.shape
        raise

    # Could do one 3D scipy.interpolate.griddata interpolation, but it
    # appears to be much slower than just doing a 2D for each month. Probably
    # takes up too much memory.

    try:
        interpolated = np.zeros((len(times), len(args['locations']) + 1))

        print '\t\tPID %d / File %d: Performing interpolations.' % (cpu_id, args['id'])
        for t, time in enumerate(times):
            interpolated[t, 0] = time
            interpolated[t, 1:] = scipy.interpolate.griddata(
                latlons, var[t], args['locations']
            )
    except:
        print 'Error interpolating:', args, lats.shape, lons.shape, latlons.shape, var.shape, times.shape
        raise

    print '\t\tPID %d / File %d: Writing %s.' % (cpu_id, args['id'], args['npy'])

    if not os.path.exists(os.path.dirname(args['npy'])):
        os.makedirs(os.path.dirname(args['npy']))

    np.save(args['npy'], interpolated)


def interpolate_variable(
    source_path, out_path, project, locations, experiments, variable,
    force=False, cpus=None
):
    """
    Opens ESGF NetCDF files from a list of experiments in the specified source
    path and interpolates the value of a given variable for a specified list of
    locations.

    Places things in out_path/project/experiment/[blah].csv. Only if they don't
    exist.

    :param source_path (str): source file path to all ESGF data. Should contain
        contain subdirectories for each project (e.g. CMIP5, CORDEX, ana4MIPs,
        etc.)
    :param out_path (str): output path for interpolated CSV files.
    :param project (str): project name (CMIP5, CORDEX, ana4MIPs, etc.)
    :param locations (np.ndarray): (n, 2) array of n latitude, longitude pairs
        at which to interpolate the specified variable.
    :param experiments (list): List of strings of experiment names. Under
        source_path/project/, there should be a subdirectory corresponding to
        each experiment.
    :param variable (str): ESGF variable to interpolate (e.g. tas).
    :param force (bool): Whether or not to force re-interpolation if the
        interpolated output file already exists.
    """

    print 'Interpolating %s.' % project

    for experiment in experiments:
        experiment_path = os.path.join(source_path, project, experiment)

        netcdf_files = [
            f for f in os.listdir(experiment_path)
            if f.endswith(('.nc', '.nc4'))
        ]

        npyfiles = [
            os.path.join(
                out_path,
                project,
                experiment,
                os.path.splitext(ncfile)[0] + '.npy'
            )
            for ncfile in netcdf_files
        ]

        if cpus is None:
            cpus = max(1, int(os.environ.get(
                'INTERP_CPUS',
                multiprocessing.cpu_count() - 1
            )))

        ncnpy = [
            (nc, npy)
            for (nc, npy) in zip(netcdf_files, npyfiles)
            if force or not os.path.exists(npy)
        ]

        print '\t%s (%d sources, %d to process)' % (
            experiment_path, len(netcdf_files), len(ncnpy)
        )

        if len(ncnpy):
            mapfun = map if cpus < 2 else multiprocessing.Pool(cpus).map

            mapfun(interpolate_variable_worker, [
                {
                    'npy': npy,
                    'netcdf': os.path.join(experiment_path, ncfile),
                    'locations': locations,
                    'var': variable,
                    'id': i,
                }
                for i, (ncfile, npy) in enumerate(ncnpy)
            ])


def transform_csv(data, model_id):
    """
    For a dataset for one particular model, create an array with rows formatted
    as follows:
        [year] [month] [location id] [model id] [temp]

    Input data is one CSV per model, with rows of the format:
        [date] [temp @ location 0] [temp @ location 1] . . .

    Dates are expressed as the number of days past 1949-12-01.
    """

    return np.vstack([
        np.column_stack((
            np.vectorize(makeyear)(data[:, 0]),
            np.vectorize(makemonth)(data[:, 0]),
            np.full_like(data[:, 0], l),
            np.full_like(data[:, 0], model_id),
            data[:, l+1]
        ))
        for l in range(data.shape[1] - 1)
    ])


def load_winteravg(data_path, experiments, force=False):
    expdata = dict()

    for experiment in experiments:
        experiment_path = os.path.join(data_path, experiment)
        winter_path = os.path.join(experiment_path, 'winter.npz')

        print 'Extracting winter averages from %s.' % experiment_path

        if force or not os.path.exists(winter_path):
            file_paths = glob.glob('%s/*.npy' % experiment_path)
            print '\t%d files.' % len(file_paths)

            # Get everything as a giant array with rows formatted as:
            #   [year] [month] [location_id] [model_id] [temp]
            data = np.vstack([
                transform_csv(np.load(path), model_id)
                for model_id, path in enumerate(file_paths)
            ])

            # Delete months we don't care about.
            cyear, cmonth, cloc, cmdl, ctemp = range(5)
            data = data[
                np.any([data[:, cmonth] == m for m in (1, 2, 3, 12)], axis=0)
            ]
            uniq_year = np.array(sorted(np.unique(data[:, cyear])), dtype=int)
            uniq_mdl = np.array(sorted(np.unique(data[:, cmdl])), dtype=int)
            uniq_loc = np.array(sorted(np.unique(data[:, cloc])), dtype=int)

            # (# locations, # years, # models)
            expdata[experiment] = dict(
                models=[modelname(path) for path in file_paths],
                years=uniq_year,
                temps=np.array([
                    [
                        [
                            np.mean(lym_data[:, ctemp])
                            for (m, lym_data) in [
                                (m, ly_data[ly_data[:, cmdl] == m])
                                for m in uniq_mdl
                            ]
                        ] for (y, ly_data) in [
                            (y, l_data[l_data[:, cyear] == y])
                            for y in uniq_year
                        ]
                    ] for (l, l_data) in [
                        (l, data[data[:, cloc] == l])
                        for l in uniq_loc
                    ]
                ])
            )

            np.savez(winter_path, **expdata[experiment])
        else:
            expdata[experiment] = np.load(winter_path)

    return expdata