import time
import PseudoNetCDF as pnc
import pandas as pd
import numpy as np
from pykrige.uk import UniversalKriging
from scipy.stats.mstats import linregress
from loadobs import aqsdf, train_and_testdfs, gf
from opts import cfg
import os
import argparse

parser = argparse.ArgumentParser()
parser.add_argument('--validate', default=0, type=int)

args = parser.parse_args()

transform = cfg['transform']
transformforward = cfg['transformforward']
transformbackward = cfg['transformbackward']

cmaqkey = cfg['model_inkey']
aqskey = cfg['obs_key']

if args.validate == 0:
    alldf = aqsdf
    suffix = 'prod'
else:
    alldf = train_and_testdfs(args.validate)['train']
    suffix = f'test{args.validate:02}'

gridx = np.arange(gf.NCOLS, dtype='f') * gf.XCELL + gf.XCELL / 2
gridy = np.arange(gf.NROWS, dtype='f') * gf.YCELL + gf.YCELL / 2

atmpl = cfg['model_template']
outtmpl = cfg['output_template']

for thisdate, ddf in alldf.groupby(['date']):
    cmaqpath = thisdate.strftime(atmpl)
    qf = pnc.pncopen(cmaqpath, format='ioapi')

    # m2path = thisdate.strftime('METCRO2D.%Y%m%d.nc')
    # mf = pnc.pncopen(m2path, format='ioapi')

    times = pd.to_datetime(
        [t.replace(tzinfo=None) for t in qf.getTimes()]
    )
    ti = (times == thisdate).argmax()
    zqf = qf.variables[cmaqkey][ti]

    for querykey, querystr in cfg['query_definitions'].items():
        print(querykey, flush=True)
        times = []
        times.append(time.time())
        krig_opts = cfg['krig_opts'].copy()
        outpath = thisdate.strftime(outtmpl.format(**globals()))
        outpath = outpath

        if os.path.exists(outpath):
            print(f'Keeping {outpath}')
            continue

        querystring = querystr.format(**cfg)
        df = ddf.query(querystring).copy()
        print('Obs shape', df.shape, flush=True)
        print(aqskey, 'mean', df[aqskey].mean(), flush=True)

        i, j = df.I.values, df.J.values
        # CMAQ
        df['Q'] = zqf[0, j, i]

        # Transformed CMAQ
        df['xQ'] = transformforward(df.Q)
        df['xO'] = transformforward(df[aqskey].values)
        # Linear regression
        lr = linregress(df.xQ.values, df.xO.values)

        # X is transformed, so Y has transformed units
        df['xY'] = (lr.slope * df.xQ + lr.intercept)
        df['xYres'] = df['xY'] - df['xO']
        print('linear fit', 'mean', transformbackward(df['xY']).mean())

        # Temperature
        # df['T'] = zqf[0, j, i]

        ###########################################################
        # Create the 2D universal kriging object and solves for the
        # two-dimension kriged error and variance.
        print('Init UK', flush=True)
        uk = UniversalKriging(
            df.X.values.astype('f'),
            df.Y.values.astype('f'),
            df.xYres.values,
            **krig_opts
        )
        print('Exec UK', flush=True)

        pxkerr, pxsdvar = uk.execute(
            "points", df.X.values.astype('f'), df.Y.values.astype('f')
        )
        print('UK at points', 'mean', transformbackward(df.xY - pxkerr).mean())

        # Calculate UniversalKriging at all grid points
        # Retruns the transformed error term and the variance of
        # the error term
        xkerr, xsdvar = uk.execute("grid", gridx, gridy)
        # When duplicate x,y coordinates, the variance can be negative
        # masking negative valuse and replacing with 0
        mxsdvar = np.ma.masked_less(xsdvar, 0)
        ninvalid = mxsdvar.mask.sum()

        xsderr = mxsdvar.filled(0)**0.5
        if ninvalid > 0:
            print('*** WARNING ****')
            print('Negative variances found. Replacing with 0.')
            print('xVAR (min, max):', xsdvar.min(), xsdvar.max())
            print('N:', mxsdvar.mask.sum())
            print('@:', np.where(mxsdvar.mask))
            print('After masking')
            print('xSD (min, max):', xsderr.min(), xsderr.max())
            print('*** WARNING ****')

        # Convert kriged error to kriged output
        xQ2d = transformforward(zqf[0])
        xY2d = xQ2d * lr.slope + lr.intercept
        kout = transformbackward(xY2d - xkerr)
        print('UK err at grid', 'mean', transformbackward(xkerr).mean())
        print('UK out at grid', 'mean', kout.mean())
        
        print('Save...', flush=True)
        # Prepare template output file
        outf = gf.renameVariables(DUMMY='UK_TOTAL')

        # Prepare UK variable
        ukvar = outf.variables['UK_TOTAL']
        ukvar[:] = np.nan
        ukvar.long_name = 'UnivKrigOutput'
        ukvar.var_desc = (
            'UnivKrig = untransform(xY + UK(xY - xO));'
            + ' where x indicates transformed'
        )
        ukvar.units = 'ppb'

        ukevar = outf.copyVariable(
            outf.variables['UK_TOTAL'], key='UK_ERROR', withdata=False
        )
        ukevar[:] = np.nan
        ukevar.long_name = 'UnivKrig of residual'
        ukevar.var_desc = (
            'UnivKrig = untransform(UK(xY - xO)); where x indicates'
            + ' transformed'
        )
        ukevar.units = 'ppb'

        # Prepare UK standard deviation variable
        uksdvar = outf.copyVariable(ukvar, key='SD', withdata=False)
        uksdvar.units = ukvar.units
        uksdvar.var_desc = 'untransform(stddev(transform(UK))'

        # Prepare CMAQ variable
        cmaqvar = outf.copyVariable(ukvar, key='Q', withdata=False)
        cmaqvar.long_name = 'CMAQ'
        cmaqvar.var_desc = 'CMAQ prediction'
        cmaqvar.units = getattr(
            qf.variables[cmaqkey], 'units', 'unknown'
        ).strip()

        # Prepare Linear Fit of CMAQ to Obs variable
        linvar = outf.copyVariable(ukvar, key='Y', withdata=False)
        linvar.long_name = 'mCMAQplB'
        linvar.units = cmaqvar.units
        linvar.var_desc = (
            f'untransform({lr.slope} transform(CMAQ) + {lr.intercept})'
        )

        # Convert UK and SD data to original units
        kerr = transformbackward(xkerr)
        sderr = transformbackward(xsderr)

        Y2d = transformbackward(xY2d)
        # Populate file data
        ukvar[0, 0] = kout[:]
        # square transform backward loses sign of kerr[:]
        # so the error in units is calculated by difference.
        ukevar[0, 0] = Y2d[:] - kout[:]
        uksdvar[0, 0] = sderr[:]
        cmaqvar[0, 0] = zqf[0]
        linvar[0, 0] = Y2d[:]

        # FILEDESC
        filedesc = f"""pykrige.UniversalKriging
Model: {cmaqpath}
AQS Query: {querystring}
Date: {thisdate}
Transformation: {transform}
Model function: {uk.variogram_model}({uk.variogram_model_parameters});
Statistics: {uk.get_statistics()}
Lags: {uk.lags}
Semivariance: {uk.semivariance}
Variance Model: {uk.variogram_function(uk.variogram_model_parameters, uk.lags)}
"""
        setattr(outf, 'FILEDESC', filedesc)
        outf.lags = uk.lags
        outf.empirical_semivariance = uk.semivariance
        outf.predicted_semivariance = uk.variogram_function(
            uk.variogram_model_parameters, uk.lags
        )
        # Save file to disk
        outf.save(outpath, complevel=1, verbose=0)
        times.append(time.time())
        timesegs = np.diff(times)
        totaltime = np.sum(timesegs)
        print(querykey, f'{totaltime:.1f} seconds')
