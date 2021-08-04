import sounddevice as sd
from scipy.signal import chirp, unit_impulse, butter, sosfilt, resample_poly
import scipy.io.wavfile as wave
from numpy.fft import fft, ifft, rfft, irfft
import numpy as np
import time


def deconv(x, y):

    # zero padding
    input_length = np.size(y)
    n = np.ceil(np.log2(input_length)) + 1
    padded_length = int(pow(2, n))
    num_zeros_to_append = padded_length - input_length

    x = np.pad(x, (0, num_zeros_to_append))
    y = np.pad(y, (0, num_zeros_to_append))

    # deconvolution
    h = ifft(fft(y) / fft(x)).real
    # truncate and window
    h = h[0:input_length]

    # squared cosine fade
    fadeout_length = 2000
    fade_tmp = np.cos(np.linspace(0, np.pi / 2, fadeout_length)) ** 2
    window = np.ones(np.size(h))
    window[np.size(window) - fadeout_length: np.size(window)] = fade_tmp
    h = h * window

    return h

# deconvolution method similar to AKdeconv() from the AKtools matlab toolbox
def deconvolve(x, y, fs, max_inv_dyn=None, lowpass=None, highpass=None):
    input_length = np.size(x)
    n = np.ceil(np.log2(input_length)) + 1
    N_fft = int(pow(2, n))

    # transform
    X_f = rfft(x, N_fft)
    Y_f = rfft(y, N_fft)

    # invert input signal
    X_inv = 1 / X_f

    if max_inv_dyn is not None:
        # identify bins that exceed max inversion dynamic
        min_mag = np.min(np.abs(X_inv))
        mag_limit = min_mag * pow(10, np.abs(max_inv_dyn) / 20)
        ids_exceed = np.where(abs(X_inv) > mag_limit)

        # clip magnitude and leave phase untouched
        X_inv[ids_exceed] = mag_limit * np.exp(1j * np.angle(X_inv[ids_exceed]))

    if lowpass is not None or highpass is not None:
        # make fir filter by pushing a dirac through a butterworth SOS (multiple times)
        lp_filter = hp_filter = unit_impulse(N_fft)

        # lowpass
        if lowpass is not None:
            sos_lp = butter(lowpass[1], lowpass[0], 'lowpass', fs=fs, output='sos')
            for i in range(lowpass[2]):
                lp_filter = sosfilt(sos_lp, lp_filter)
        lp_filter = rfft(lp_filter)

        # highpass
        if highpass is not None:
            sos_hp = butter(highpass[1], highpass[0], 'highpass', fs=fs, output='sos')
            for i in range(highpass[2]):
                hp_filter = sosfilt(sos_hp, hp_filter)
        hp_filter = rfft(hp_filter)

        lp_hp_filter = hp_filter * lp_filter

        # apply filter
        X_inv = X_inv * lp_hp_filter

    # deconvolve
    H = Y_f * X_inv

    # backward transform
    h = irfft(H, N_fft)

    # truncate to original length
    h = h[:input_length]

    return h

def make_excitation_sweep(fs, num_channels=1, d_sweep_sec=3, d_post_silence_sec=1, f_start=20, f_end=20000, amp_db=-20, fade_out_samples=0):

    amplitude_lin = 10 ** (amp_db / 20)

    # make sweep
    t_sweep = np.linspace(0, d_sweep_sec, int(d_sweep_sec * fs))
    sweep = amplitude_lin * chirp(t_sweep, f0=f_start, t1=d_sweep_sec, f1=f_end, method='logarithmic', phi=90)

    # squared cosine fade
    fade_tmp = np.cos(np.linspace(0, np.pi / 2, fade_out_samples)) ** 2
    window = np.ones(np.size(sweep, 0))
    window[np.size(window) - fade_out_samples: np.size(window)] = fade_tmp
    sweep = sweep * window

    pre_silence = int(fs * 0.01) # 10msec post silence for safety while playback
    post_silence = int(fs * d_post_silence_sec)


    excitation = np.pad(sweep, (pre_silence, post_silence))

    excitation = np.tile(excitation, (num_channels, 1))  # make stereo or more, for out channels 1 & 2
    excitation = np.transpose(excitation).astype(np.float32)

    return excitation



class Measurement():

    def __init__(self):

        self.dummy_debugging = False

        if sd.default.samplerate is None:
            sd.default.samplerate = 48000

        self.sweep_parameters = {
            'sweeplength_sec': 3.0,
            'post_silence_sec': 1.5,
            'f_start': 100,
            'f_end': 22000,
            'amp_db': -20.0,
            'fade_out_samples': 200
        }

        #read sound files
        self.sound_success_fs, self.sound_success_singlechannel = wave.read('resources/soundfx_success.wav')
        self.sound_failed_fs, self.sound_failed_singlechannel = wave.read('resources/soundfx_failed.wav')

        # normalize and adjust level
        self.sound_failed_singlechannel = self.sound_failed_singlechannel * 0.05 / 32768
        self.sound_success_singlechannel = self.sound_success_singlechannel * 0.05 / 32768

        # default channels at startup
        self.channel_layout_input = [0, 1, -1]
        self.channel_layout_output = [0, 1, -1]
        self.num_input_channels_used = 2
        self.num_output_channels_used = 2
        self.feedback_loop_used = False

        self.sweep_mono = None
        self.sweep_hpc_mono = None
        self.excitation = None
        self.excitation_hpc = None
        self.sound_success = None
        self.sound_failed = None

        self.recorded_sweep_l = None
        self.recorded_sweep_r = None
        self.feedback_loop = None

        self.prepare_audio()


    def set_sweep_parameters(self, d_sweep_sec, d_post_silence_sec, f_start, f_end, amp_db, fade_out_samples):
        self.sweep_parameters['sweeplength_sec'] = d_sweep_sec
        self.sweep_parameters['post_silence_sec'] = d_post_silence_sec
        self.sweep_parameters['f_start'] = f_start
        self.sweep_parameters['f_end'] = f_end
        self.sweep_parameters['amp_db'] = amp_db
        self.sweep_parameters['fade_out_samples'] = fade_out_samples

        self.prepare_audio()

    def get_sweep_parameters(self):
        return self.sweep_parameters

    def get_samplerate(self):
        return sd.default.samplerate

    def set_channel_layout(self, in_channels, out_channels):
        """
        To be called when the audio channel layout has changed. Setting a channel to -1 disables it

        Parameters
        ----------
        in_channels : list of 3 ints with zero-indexed channel id, (-1 indicates disabled)
            1st entry: input channel left ear
            2nd entry: input channel right ear
            3rd entry: input channel feedback loop

        out_channels: list of 3 ints with zero-indexed channel id, (-1 indicates disabled)
            1st entry: output channel 1
            2nd entry: output channel 2
            3rd entry: output channel feedback loop

        """

        if out_channels[2] < 0 or in_channels[2] < 0:
            self.feedback_loop_used = False
            in_channels[2] = -1
            out_channels[2] = -1
        else:
            self.feedback_loop_used = True

        self.channel_layout_input = in_channels
        self.channel_layout_output = out_channels
        self.num_output_channels_used = max(out_channels) + 1
        self.num_input_channels_used = max(in_channels) + 1


        self.prepare_audio()

    def set_samplerate(self, fs=None):
        '''To be called when samplerate change occured.
        Parameters
        ----------
        fs :    New samplerate (int, optional) if not specified, that the samplerate has been set elsewhere via
                sonddevice.default.samplerate'''

        if fs is None:
            if sd.default.samplerate is None:
                sd.default.samplerate = 48000
        else:
            sd.default.samplerate = fs


        self.prepare_audio()

    def prepare_audio(self):
        # whenever something changes regarding the audio signals, recompute everything. Could be more efficient, but this
        # way the code remains simple
        fs = sd.default.samplerate

        # Make the sweep signals

        self.sweep_mono = make_excitation_sweep(fs=fs,
                                                d_sweep_sec=self.sweep_parameters['sweeplength_sec'],
                                                d_post_silence_sec=self.sweep_parameters['post_silence_sec'],
                                                f_start=self.sweep_parameters['f_start'],
                                                f_end=self.sweep_parameters['f_end'],
                                                amp_db=self.sweep_parameters['amp_db'],
                                                fade_out_samples=self.sweep_parameters['fade_out_samples'])

        if self.dummy_debugging:
            self.sweep_mono = make_excitation_sweep(fs=fs, f_start=100, d_sweep_sec=0.01, d_post_silence_sec=0.01)

        self.sweep_hpc_mono = make_excitation_sweep(fs=fs, d_sweep_sec=2)

        # Adjust samplerate on the audio files
        sound_success_src = resample_poly(self.sound_success_singlechannel, round(fs), self.sound_success_fs)
        sound_failed_src = resample_poly(self.sound_failed_singlechannel, round(fs), self.sound_failed_fs)


        # Since portaudio does not support the useage of individual channels, the channel assignment is "faked" by
        # creating a multichannel audio file for playrec() and only playing the sweep on the selected output channels.
        # On the other side, only the selected input channels are used from the recorded multichannel wave file
        out_channels = self.channel_layout_output

        if not self.feedback_loop_used:
            out_channels = out_channels[0:2]


        # make multichannel audiofile and assign the sweep to designated channels
        self.excitation = np.zeros([np.size(self.sweep_mono, 0), self.num_output_channels_used])
        self.excitation[:, out_channels] = self.sweep_mono

        # same for HPC measurement
        self.excitation_hpc = np.zeros([np.size(self.sweep_hpc_mono, 0), self.num_output_channels_used])
        self.excitation_hpc[:, out_channels] = self.sweep_hpc_mono

        # also for the sound fx
        self.sound_success = np.zeros([np.size(sound_success_src, 0), self.num_output_channels_used])
        self.sound_success[:, out_channels[0:2]] = np.expand_dims(sound_success_src, axis=1)
        self.sound_failed = np.zeros([np.size(sound_failed_src, 0), self.num_output_channels_used])
        self.sound_failed[:, out_channels[0:2]] = np.expand_dims(sound_failed_src, axis=1)


    def play_sound(self, success):

        # little workaround of a problem with using ASIO from multiple threads
        # https://stackoverflow.com/questions/39858212/python-sounddevice-play-on-threads
        default_device = sd.query_devices(sd.default.device[0])
        default_api = sd.query_hostapis(default_device['hostapi'])
        if default_api['name'] == 'ASIO':
            sd._terminate()
            sd._initialize()

        if success:
            sd.play(self.sound_success)
        else:
            sd.play(self.sound_failed)
        sd.wait()

    def interrupt_measurement(self):
        sd.stop()

    def single_measurement(self, type=None):

        # little workaround of a problem with using ASIO from multiple threads
        # https://stackoverflow.com/questions/39858212/python-sounddevice-play-on-threads
        default_device = sd.query_devices(sd.default.device[0])
        default_api = sd.query_hostapis(default_device['hostapi'])
        if default_api['name'] == 'ASIO':
            sd._terminate()
            sd._initialize()


        self.recorded_sweep_l = []
        self.recorded_sweep_r = []
        self.feedback_loop = []

        if type is 'hpc':
            excitation = self.excitation_hpc
        else:
            excitation = self.excitation

        if not self.dummy_debugging:
            time.sleep(0.3)

        try:
            sd.check_input_settings(channels=self.num_input_channels_used)
            sd.check_output_settings(channels=self.num_output_channels_used)
        except:
            print("Audio hardware error! Too many channels or unsupported samplerate")
            return

        # do measurement
        recorded = sd.playrec(excitation, channels=self.num_input_channels_used)
        sd.wait()

        # get the recorded signals
        if self.channel_layout_input[0] >= 0:
            self.recorded_sweep_l = recorded[:, self.channel_layout_input[0]]
        else:
            self.recorded_sweep_l = np.zeros(np.size(recorded, 0))

        if self.channel_layout_input[1] >= 0:
            self.recorded_sweep_r = recorded[:, self.channel_layout_input[1]]
        else:
            self.recorded_sweep_r = np.zeros(np.size(recorded, 0))

        if self.feedback_loop_used:
            self.feedback_loop = recorded[:, self.channel_layout_input[2]]
            if abs(self.feedback_loop.max()) < 0.0001:
                self.feedback_loop = np.random.random_sample(self.feedback_loop.shape) * 0.000001  # to avoid zero-division errors
        else:
            # if no FB loop, copy from original excitation sweep
            if type is 'hpc':
                self.feedback_loop = self.sweep_hpc_mono[:, 0]
            else:
                self.feedback_loop = self.sweep_mono[:, 0]




    def get_recordings(self):
        return [self.recorded_sweep_l, self.recorded_sweep_r, self.feedback_loop]

    def get_irs(self, rec_l=None, rec_r=None, fb_loop=None, deconv_fc_hp = None, deconv_fc_lp = None):
        try:
            if rec_l is None:
                rec_l = self.recorded_sweep_l
            if rec_r is None:
                rec_r = self.recorded_sweep_r
            if fb_loop is None:
                fb_loop = self.feedback_loop
            if deconv_fc_hp is None:
                deconv_fc_hp = self.sweep_parameters['f_start'] * 2
            if deconv_fc_lp is None:
                deconv_fc_lp = 20000

            ir_l = deconvolve(fb_loop, rec_l, self.fs, lowpass=[deconv_fc_lp, 4, 2], highpass=[deconv_fc_hp, 4, 2])
            ir_r = deconvolve(fb_loop, rec_r, self.fs, lowpass=[deconv_fc_lp, 4, 2], highpass=[deconv_fc_hp, 4, 2])
            return [ir_l, ir_r]
        except:
            return
