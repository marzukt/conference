#!/usr/bin/env python

"""
conference.py -- Udacity conference server-side Python App Engine API;
    uses Google Cloud Endpoints

$Id: conference.py,v 1.25 2014/05/24 23:42:19 wesc Exp wesc $

created by wesc on 2014 apr 21

"""

__author__ = 'wesc+api@google.com (Wesley Chun)'


from datetime import datetime, time

import endpoints
from protorpc import messages
from protorpc import message_types
from protorpc import remote

from google.appengine.api import memcache
from google.appengine.api import taskqueue
from google.appengine.ext import ndb

from models import ConflictException
from models import Profile
from models import ProfileMiniForm
from models import ProfileForm
from models import StringMessage
from models import BooleanMessage
from models import Conference
from models import ConferenceForm
from models import ConferenceForms
from models import ConferenceQueryForm
from models import ConferenceQueryForms
from models import TeeShirtSize
from models import Session
from models import SessionForm
from models import SessionForms
from models import FeaturedSpeakerForm

from settings import WEB_CLIENT_ID
from settings import ANDROID_CLIENT_ID
from settings import IOS_CLIENT_ID
from settings import ANDROID_AUDIENCE
from google.net.proto.ProtocolBuffer import ProtocolBufferDecodeError
import logging
from utils import getUserId

EMAIL_SCOPE = endpoints.EMAIL_SCOPE
API_EXPLORER_CLIENT_ID = endpoints.API_EXPLORER_CLIENT_ID
MEMCACHE_ANNOUNCEMENTS_KEY = "RECENT_ANNOUNCEMENTS"
MEMCACHE_FEATURED_SPEAKER_KEY = "FEATURED_SPEAKER"
ANNOUNCEMENT_TPL = ('Last chance to attend! The following conferences '
                    'are nearly sold out: %s')
# - - - - - - - - - - - - - - - - - - - - - - - - - - - - - -

DEFAULTS = {
    "city": "Default City",
    "maxAttendees": 0,
    "seatsAvailable": 0,
    "topics": [ "Default", "Topic" ],
}

OPERATORS = {
            'EQ':   '=',
            'GT':   '>',
            'GTEQ': '>=',
            'LT':   '<',
            'LTEQ': '<=',
            'NE':   '!='
            }

FIELDS =    {
            'CITY': 'city',
            'TOPIC': 'topics',
            'MONTH': 'month',
            'MAX_ATTENDEES': 'maxAttendees',
            }

CONF_GET_REQUEST = endpoints.ResourceContainer(
    message_types.VoidMessage,
    websafeConferenceKey=messages.StringField(1),
)

CONF_POST_REQUEST = endpoints.ResourceContainer(
    ConferenceForm,
    websafeConferenceKey=messages.StringField(1),
)

SESS_POST_REQUEST = endpoints.ResourceContainer(
    SessionForm,
    websafeConferenceKey=messages.StringField(1),
)
SESS_BYTYPE_GET_REQUEST = endpoints.ResourceContainer(
    SessionForm,
    websafeConferenceKey=messages.StringField(1),
    typeOfSession=messages.StringField(2),
)

SESS_BYSPEAK_GET_REQUEST = endpoints.ResourceContainer(
    SessionForm,
    speaker=messages.StringField(1),
)

SESS_WISHLIST_POST_REQUEST = endpoints.ResourceContainer(
    SessionForm,
    websafeSessionKey=messages.StringField(1),
)

SHORT_SESSION_GET_REQUEST = endpoints.ResourceContainer(
        duration = messages.IntegerField(1)
    )

SESS_HIGHLIGHT_GET_REQUEST = endpoints.ResourceContainer(
    highlights = messages.StringField(1),
)
# - - - - - - - - - - - - - - - - - - - - - - - - - - - - - -


@endpoints.api(name='conference', version='v1', audiences=[ANDROID_AUDIENCE],
    allowed_client_ids=[WEB_CLIENT_ID, API_EXPLORER_CLIENT_ID, ANDROID_CLIENT_ID, IOS_CLIENT_ID],
    scopes=[EMAIL_SCOPE])
class ConferenceApi(remote.Service):
    """Conference API v0.1"""

# - - - Conference objects - - - - - - - - - - - - - - - - -

    def _copyConferenceToForm(self, conf, displayName):
        """Copy relevant fields from Conference to ConferenceForm."""
        cf = ConferenceForm()
        for field in cf.all_fields():
            if hasattr(conf, field.name):
                # convert Date to date string; just copy others
                if field.name.endswith('Date'):
                    setattr(cf, field.name, str(getattr(conf, field.name)))
                else:
                    setattr(cf, field.name, getattr(conf, field.name))
            elif field.name == "websafeKey":
                setattr(cf, field.name, conf.key.urlsafe())
        if displayName:
            setattr(cf, 'organizerDisplayName', displayName)
        cf.check_initialized()
        return cf


    def _createConferenceObject(self, request):
        """Create or update Conference object, returning ConferenceForm/request."""
        # preload necessary data items
        user = endpoints.get_current_user()
        if not user:
            raise endpoints.UnauthorizedException('Authorization required')
        user_id = getUserId(user)

        if not request.name:
            raise endpoints.BadRequestException("Conference 'name' field required")

        # copy ConferenceForm/ProtoRPC Message into dict
        data = {field.name: getattr(request, field.name) for field in request.all_fields()}
        del data['websafeKey']
        del data['organizerDisplayName']

        # add default values for those missing (both data model & outbound Message)
        for df in DEFAULTS:
            if data[df] in (None, []):
                data[df] = DEFAULTS[df]
                setattr(request, df, DEFAULTS[df])

        # convert dates from strings to Date objects; set month based on start_date
        if data['startDate']:
            data['startDate'] = datetime.strptime(data['startDate'][:10], "%Y-%m-%d").date()
            data['month'] = data['startDate'].month
        else:
            data['month'] = 0
        if data['endDate']:
            data['endDate'] = datetime.strptime(data['endDate'][:10], "%Y-%m-%d").date()

        # set seatsAvailable to be same as maxAttendees on creation
        if data["maxAttendees"] > 0:
            data["seatsAvailable"] = data["maxAttendees"]
        # generate Profile Key based on user ID and Conference
        # ID based on Profile key get Conference key from ID
        p_key = ndb.Key(Profile, user_id)
        c_id = Conference.allocate_ids(size=1, parent=p_key)[0]
        c_key = ndb.Key(Conference, c_id, parent=p_key)
        data['key'] = c_key
        data['organizerUserId'] = request.organizerUserId = user_id

        # create Conference, send email to organizer confirming
        # creation of Conference & return (modified) ConferenceForm
        Conference(**data).put()
        taskqueue.add(params={'email': user.email(),
            'conferenceInfo': repr(request)},
            url='/tasks/send_confirmation_email'
        )
        return request


    @ndb.transactional()
    def _updateConferenceObject(self, request):
        user = endpoints.get_current_user()
        if not user:
            raise endpoints.UnauthorizedException('Authorization required')
        user_id = getUserId(user)

        # copy ConferenceForm/ProtoRPC Message into dict
        data = {field.name: getattr(request, field.name) for field in request.all_fields()}

        # update existing conference
        conf = ndb.Key(urlsafe=request.websafeConferenceKey).get()
        # check that conference exists
        if not conf:
            raise endpoints.NotFoundException(
                'No conference found with key: %s' % request.websafeConferenceKey)

        # check that user is owner
        if user_id != conf.organizerUserId:
            raise endpoints.ForbiddenException(
                'Only the owner can update the conference.')

        # Not getting all the fields, so don't create a new object; just
        # copy relevant fields from ConferenceForm to Conference object
        for field in request.all_fields():
            data = getattr(request, field.name)
            # only copy fields where we get data
            if data not in (None, []):
                # special handling for dates (convert string to Date)
                if field.name in ('startDate', 'endDate'):
                    data = datetime.strptime(data, "%Y-%m-%d").date()
                    if field.name == 'startDate':
                        conf.month = data.month
                # write to Conference object
                setattr(conf, field.name, data)
        conf.put()
        prof = ndb.Key(Profile, user_id).get()
        return self._copyConferenceToForm(conf, getattr(prof, 'displayName'))


    @endpoints.method(ConferenceForm, ConferenceForm, path='conference',
            http_method='POST', name='createConference')
    def createConference(self, request):
        """Create new conference."""
        return self._createConferenceObject(request)


    @endpoints.method(CONF_POST_REQUEST, ConferenceForm,
            path='conference/{websafeConferenceKey}',
            http_method='PUT', name='updateConference')
    def updateConference(self, request):
        """Update conference w/provided fields & return w/updated info."""
        return self._updateConferenceObject(request)


    @endpoints.method(CONF_GET_REQUEST, ConferenceForm,
            path='conference/{websafeConferenceKey}',
            http_method='GET', name='getConference')
    def getConference(self, request):
        """Return requested conference (by websafeConferenceKey)."""
        # get Conference object from request; bail if not found
        conf = ndb.Key(urlsafe=request.websafeConferenceKey).get()
        if not conf:
            raise endpoints.NotFoundException(
                'No conference found with key: %s' % request.websafeConferenceKey)
        prof = conf.key.parent().get()
        # return ConferenceForm
        return self._copyConferenceToForm(conf, getattr(prof, 'displayName'))


    @endpoints.method(message_types.VoidMessage, ConferenceForms,
            path='getConferencesCreated',
            http_method='POST', name='getConferencesCreated')
    def getConferencesCreated(self, request):
        """Return conferences created by user."""
        # make sure user is authed
        user = endpoints.get_current_user()
        if not user:
            raise endpoints.UnauthorizedException('Authorization required')
        user_id = getUserId(user)

        # create ancestor query for all key matches for this user
        confs = Conference.query(ancestor=ndb.Key(Profile, user_id))
        prof = ndb.Key(Profile, user_id).get()
        # return set of ConferenceForm objects per Conference
        return ConferenceForms(
            items=[self._copyConferenceToForm(conf, getattr(prof, 'displayName')) for conf in confs]
        )


    def _getQuery(self, request):
        """Return formatted query from the submitted filters."""
        q = Conference.query()
        inequality_filter, filters = self._formatFilters(request.filters)

        # If exists, sort on inequality filter first
        if not inequality_filter:
            q = q.order(Conference.name)
        else:
            q = q.order(ndb.GenericProperty(inequality_filter))
            q = q.order(Conference.name)

        for filtr in filters:
            if filtr["field"] in ["month", "maxAttendees"]:
                filtr["value"] = int(filtr["value"])
            formatted_query = ndb.query.FilterNode(filtr["field"], filtr["operator"], filtr["value"])
            q = q.filter(formatted_query)
        return q


    def _formatFilters(self, filters):
        """Parse, check validity and format user supplied filters."""
        formatted_filters = []
        inequality_field = None

        for f in filters:
            filtr = {field.name: getattr(f, field.name) for field in f.all_fields()}

            try:
                filtr["field"] = FIELDS[filtr["field"]]
                filtr["operator"] = OPERATORS[filtr["operator"]]
            except KeyError:
                raise endpoints.BadRequestException("Filter contains invalid field or operator.")

            # Every operation except "=" is an inequality
            if filtr["operator"] != "=":
                # check if inequality operation has been used in previous filters
                # disallow the filter if inequality was performed on a different field before
                # track the field on which the inequality operation is performed
                if inequality_field and inequality_field != filtr["field"]:
                    raise endpoints.BadRequestException("Inequality filter is allowed on only one field.")
                else:
                    inequality_field = filtr["field"]

            formatted_filters.append(filtr)
        return (inequality_field, formatted_filters)


    @endpoints.method(ConferenceQueryForms, ConferenceForms,
            path='queryConferences',
            http_method='POST',
            name='queryConferences')
    def queryConferences(self, request):
        """Query for conferences."""
        conferences = self._getQuery(request)

        # need to fetch organiser displayName from profiles
        # get all keys and use get_multi for speed
        organisers = [(ndb.Key(Profile, conf.organizerUserId)) for conf in conferences]
        profiles = ndb.get_multi(organisers)

        # put display names in a dict for easier fetching
        names = {}
        for profile in profiles:
            names[profile.key.id()] = profile.displayName

        # return individual ConferenceForm object per Conference
        return ConferenceForms(
                items=[self._copyConferenceToForm(conf, names[conf.organizerUserId]) for conf in \
                conferences]
        )


# - - - Profile objects - - - - - - - - - - - - - - - - - - -

    def _copyProfileToForm(self, prof):
        """Copy relevant fields from Profile to ProfileForm."""
        # copy relevant fields from Profile to ProfileForm
        pf = ProfileForm()
        for field in pf.all_fields():
            if hasattr(prof, field.name):
                # convert t-shirt string to Enum; just copy others
                if field.name == 'teeShirtSize':
                    setattr(pf, field.name, getattr(TeeShirtSize, getattr(prof, field.name)))
                else:
                    setattr(pf, field.name, getattr(prof, field.name))
        pf.check_initialized()
        return pf


    def _getProfileFromUser(self):
        """Return user Profile from datastore, creating new one if non-existent."""
        # make sure user is authed
        user = endpoints.get_current_user()
        if not user:
            raise endpoints.UnauthorizedException('Authorization required')

        # get Profile from datastore
        user_id = getUserId(user)
        # create key of kind Profile using the email returned by getUserId
        p_key = ndb.Key(Profile, user_id)
        # try a retrieve a profile for the key
        profile = p_key.get()
        # create new Profile if not there
        if not profile:
            profile = Profile(
                key = p_key,
                displayName = user.nickname(),
                mainEmail= user.email(),
                teeShirtSize = str(TeeShirtSize.NOT_SPECIFIED),
            )
            profile.put()

        return profile      # return Profile


    def _doProfile(self, save_request=None):
        """Get user Profile and return to user, possibly updating it first."""
        # get user Profile
        prof = self._getProfileFromUser()

        # if saveProfile(), process user-modifyable fields
        if save_request:
            for field in ('displayName', 'teeShirtSize'):
                if hasattr(save_request, field):
                    val = getattr(save_request, field)
                    if val:
                        setattr(prof, field, str(val))
                        #if field == 'teeShirtSize':
                        #    setattr(prof, field, str(val).upper())
                        #else:
                        #    setattr(prof, field, val)
                        prof.put()

        # return ProfileForm
        return self._copyProfileToForm(prof)


    @endpoints.method(message_types.VoidMessage, ProfileForm,
            path='profile', http_method='GET', name='getProfile')
    def getProfile(self, request):
        """Return user profile."""
        return self._doProfile()


    @endpoints.method(ProfileMiniForm, ProfileForm,
            path='profile', http_method='POST', name='saveProfile')
    def saveProfile(self, request):
        """Update & return user profile."""
        return self._doProfile(request)


# - - - Session Objects - - - - - - - - - - - - - - - - - - - -

    def _createSessionObject(self, request):
        """Create Session object, returning SessionForm/request."""
        # preload necessary data items
        user = endpoints.get_current_user()
        if not user:
            raise endpoints.UnauthorizedException('Authorization required')
        user_id = getUserId(user)

        # Lookup the conference to add the session to
        # raise an error if it doesn't exist
        try:
            conf_key = ndb.Key(urlsafe=request.websafeConferenceKey)
            conf = conf_key.get()
        except ProtocolBufferDecodeError:
            raise endpoints.NotFoundException(
                'No conference found with key: {}'.format(request.websafeConferenceKey))
        except TypeError:
            raise endpoints.BadRequestException(
                'Conference key must be a string')

        # session must at least have a name
        if not request.name:
            raise endpoints.BadRequestException("Session 'name' field required")

        # copy SessionForm/ProtoRPC Message into dict
        data = {field.name: getattr(request, field.name) for field in request.all_fields()}
        del data['websafeKey']

        # check that user is the owner of the conference
        if user_id != conf.organizerUserId:
            raise endpoints.ForbiddenException(
                'Only the conference owner can add a session to the conference {}.'.format(conf.name))


        # convert date from string to Date object
        if data['date']:
            data['date'] = datetime.strptime(data['date'][:10], "%Y-%m-%d").date()
        else:
            data['date'] = getattr(conf,"startDate")
        # covert time from 24hr string format to time object
        if data['startTime']:
            data['startTime'] = datetime.strptime(data['startTime'][:6], "%H:%M").time()

        # generate a unique Session id using the parent conference key
        s_id = Session.allocate_ids(size=1, parent=conf_key)[0]
        # Generate a key using the Session ID and parent Conference key
        s_key = ndb.Key(Session, s_id, parent=conf_key)
        data['key'] = s_key

        # create Session and return modified SessionForm
        del data['websafeConferenceKey']
        session = Session(**data)
        session.put()

        # Add featured speaker update task if the session has a speaker
        if data['speaker']:
            params={'speaker': data['speaker'],
                                  'websafeConferenceKey': request.websafeConferenceKey}
            print params
            taskqueue.add(params={'speaker': data['speaker'],
                                  'websafeConferenceKey': request.websafeConferenceKey},
                    url='/tasks/set_featured_speaker'
            )

        return self._copySessionToForm(session)

    @endpoints.method(SessionForm, SessionForm,
                      path='conference/{websafeConferenceKey}/createsession',
            http_method='POST', name='createSession')
    def createSession(self, request):
        """Create new session."""
        return self._createSessionObject(request)

    @endpoints.method(SESS_POST_REQUEST, SessionForms,
            path='conference/{websafeConferenceKey}/sessions',
            http_method='GET', name='getConferenceSessions')
    def getConferenceSessions(self,request):
        """Return conference sessions for a given conference"""
        try:
            conf_key = ndb.Key(urlsafe=request.websafeConferenceKey)
        except ProtocolBufferDecodeError:
            raise endpoints.NotFoundException(
                'No conference found with key: {}'.format(request.websafeConferenceKey))
        except TypeError:
            raise endpoints.BadRequestException(
                'Conference key must be a string')

        sessions = Session.query(ancestor=conf_key)
        # sessions = Session.query(ancestor=ndb.Key(Conference,conf_key))
        # return set of SessionForm objects for the conference
        return SessionForms(
            items=[self._copySessionToForm(session) for session in sessions]
        )

    def _copySessionToForm(self, session):
        """Copy relevant fields from Session to SessionForm."""
        sf = SessionForm()
        for field in sf.all_fields():
            if hasattr(session, field.name):
                # convert Date to date string; just copy others
                if field.name in ('date','startTime'):
                    setattr(sf, field.name, str(getattr(session, field.name)))
                else:
                    setattr(sf, field.name, getattr(session, field.name))
            elif field.name == "websafeKey":
                setattr(sf, field.name, session.key.urlsafe())
        sf.check_initialized()
        return sf

    @endpoints.method(SESS_BYTYPE_GET_REQUEST, SessionForms,
            path='conference/{websafeConferenceKey}/sessions/type/{typeOfSession}',
            http_method='GET', name='getConferenceSessionsbyType')
    def getConferenceSessionsByType(self,request):
        """Return conferences sessions by type"""
        try:
            conf_key = ndb.Key(urlsafe=request.websafeConferenceKey)
        except ProtocolBufferDecodeError:
            raise endpoints.NotFoundException(
                'No conference found with key: %s' % request.websafeConferenceKey)
        except TypeError:
            raise endpoints.BadRequestException(
                'session key must be a string')

        # require a type of session be provided
        if not request.typeOfSession:
            raise endpoints.BadRequestException(
                'No session type provided')
        if request.typeOfSession == "":
            request.typeOfSession = None

        sessions = Session.query(ancestor=conf_key)\
                          .filter(Session.typeOfSession == request.typeOfSession)

        # return set of SessionForm objects
        return SessionForms(
            items=[self._copySessionToForm(session) for session in sessions]
        )

    @endpoints.method(SESS_BYSPEAK_GET_REQUEST, SessionForms,
            path='sessions/byspeaker/{speaker}',
            http_method='GET', name='getSessionsbySpeaker')
    def getSessionsBySpeaker(self,request):
        """Return all sessions by a given speaker."""
        sessions = Session.query(Session.speaker == request.speaker)

        # return a set of SessionForms
        return SessionForms(
            items=[self._copySessionToForm(session) for session in sessions]
        )

    @endpoints.method(SHORT_SESSION_GET_REQUEST, SessionForms,
            path='sessions/short',
            http_method='GET', name='getShortSessions')
    def getShortSessions(self,request):
        """Return sessions which are shorter than a given duration.
           If no value is given duration defaults to 30 minutes
        """
        if not request.duration:
            short_dur = 30
        else:
            short_dur = request.duration
        sessions = Session.query(Session.duration <= short_dur)\
                          .filter(Session.duration >= 0)

        # return set of ConferenceForm objects per Conference
        return SessionForms(
            items=[self._copySessionToForm(session) for session in sessions]
        )

    @endpoints.method(SESS_HIGHLIGHT_GET_REQUEST, SessionForms,
                      path='/sessions/highlights/{highlights}',
                      http_method = 'GET', name='getSessionsHighlights')
    def checkSessionHighlights(self,request):
        """ Return Sessions with specific highlights"""
        sessions = Session.query(Session.highlights == request.highlights)
        return SessionForms(
            items=[self._copySessionToForm(session) for session in sessions]
        )

    @endpoints.method(message_types.VoidMessage, SessionForms,
            path='sessions/earlynonworkshops',
            http_method='GET', name='getEarlyNonWorkshops')
    def getEarlyNonWorkshops(self,request):
        """Return non workshops sessions before 1900"""
        filterTime = time(19)
        filterSessionType = "workshop"
        q = Session.query()
        q = q.filter(Session.startTime <= filterTime)
        # exclude sessions with no start time
        q = q.filter(Session.startTime >= time(00))

        return SessionForms(
            items=[self._copySessionToForm(session) for session in q if session.typeOfSession != filterSessionType]
        )
#  - - - - - - - - - - - - - - - - - - - - - Session wish lists - - - - - - - - - - - - - - - - - - - - -

    @endpoints.method(SESS_WISHLIST_POST_REQUEST, BooleanMessage,
            path='sessions/{websafeSessionKey}/addtowishlist',
            http_method='POST', name='addSessionWishlist')
    def addSessionWishlist(self, request, reg=True):
        """Add a given conference session to a user's wishlist."""
        retval = None
        prof = self._getProfileFromUser() # get user Profile

        # check if conf exists given websafeSessionKey
        # get session; check that it exists
        wssk = request.websafeSessionKey
        try:
            sessionKey = ndb.Key(urlsafe=wssk)
            session = sessionKey.get()
        except ProtocolBufferDecodeError:
            raise endpoints.NotFoundException(
                'No session found with key: {}'.format(wssk))
        except TypeError:
            raise endpoints.BadRequestException(
                'session key must be a string')
        if session.key.kind() != 'Session':
            raise endpoints.BadRequestException(
                'Key is not a session key')

        # lookup parent conference
        parent_conf = session.key.parent().get()
        # check if user is registered for the parent conference
        # otherwise advise them to register for conference
        if parent_conf.key.urlsafe() not in prof.conferenceKeysToAttend:
            raise ConflictException(
                "You are not registered for the conference {}. Please register to attend".format(getattr(parent_conf,'name')))
        # check if user already has the session in their wishlist
        if sessionKey in prof.sessionKeysWishList:
            raise ConflictException(
                "You have already added this session to your wishlist")
        else:
           prof.sessionKeysWishList.append(sessionKey)
        retval = True

        # write things back to the datastore & return
        prof.put()
        return BooleanMessage(data=retval)

    @endpoints.method(message_types.VoidMessage, SessionForms,
            path='sessions/wishlist/get',
            http_method='GET', name='getSessionsInWishlist')
    def getSessionsInWishlist(self,request):
        """Get the wishlist of sessions for a user."""
        prof = self._getProfileFromUser() # get user Profile
        # get the sessions the user has added to their wishlist
        #logging.info(prof.sessionKeysWishList)
        session_keys = [session_key for session_key in prof.sessionKeysWishList]
        sessions = ndb.get_multi(session_keys)

        # return set of SessionForm objects per Session
        return SessionForms(
            items=[self._copySessionToForm(session) for session in sessions]
        )

# - - - Featured Speaker - - - - - - - - - - - - - - - - - - - -
    @staticmethod
    def _cacheFeaturedSpeaker(speaker, websafeConferenceKey):
        """Set the Featured Speaker if a speaker has more than one
        session in a conference
        """
        # check if the conference has more than one session by this speaker
        # if so add as a featured speaker
        conf_key = ndb.Key(urlsafe=websafeConferenceKey)
        sessionsBySpeaker = Session.query(ancestor=conf_key)\
                                   .filter(Session.speaker == speaker)
        if sessionsBySpeaker.count > 1:
            featuredSpeaker = {}
            featuredSpeaker['speaker'] = speaker
            featuredSpeaker['sessions'] = [session.name for session in sessionsBySpeaker]

            memcache.set(MEMCACHE_FEATURED_SPEAKER_KEY, featuredSpeaker)

    @endpoints.method(message_types.VoidMessage, FeaturedSpeakerForm,
            path='conference/featured_speaker/get',
            http_method='GET', name='getfeaturedSpeaker')
    def getFeaturedSpeaker(self,request):
        """Return the Featured Speaker from memcache."""
        featuredSpeaker = memcache.get(MEMCACHE_FEATURED_SPEAKER_KEY)
        if not featuredSpeaker:
            return StringMessage(data="")
        return FeaturedSpeakerForm(speakerName=featuredSpeaker['speaker'],
                           sessionNames = featuredSpeaker['sessions'])

# - - - Announcements - - - - - - - - - - - - - - - - - - - -

    @staticmethod
    def _cacheAnnouncement():
        """Create Announcement & assign to memcache; used by
        memcache cron job & putAnnouncement().
        """
        confs = Conference.query(ndb.AND(
            Conference.seatsAvailable <= 5,
            Conference.seatsAvailable > 0)
        ).fetch(projection=[Conference.name])

        if confs:
            # If there are almost sold out conferences,
            # format announcement and set it in memcache
            announcement = ANNOUNCEMENT_TPL % (
                ', '.join(conf.name for conf in confs))
            memcache.set(MEMCACHE_ANNOUNCEMENTS_KEY, announcement)
        else:
            # If there are no sold out conferences,
            # delete the memcache announcements entry
            announcement = ""
            memcache.delete(MEMCACHE_ANNOUNCEMENTS_KEY)

        return announcement


    @endpoints.method(message_types.VoidMessage, StringMessage,
            path='conference/announcement/get',
            http_method='GET', name='getAnnouncement')
    def getAnnouncement(self, request):
        """Return Announcement from memcache."""
        return StringMessage(data=memcache.get(MEMCACHE_ANNOUNCEMENTS_KEY) or "")


# - - - Registration - - - - - - - - - - - - - - - - - - - -

    @ndb.transactional(xg=True)
    def _conferenceRegistration(self, request, reg=True):
        """Register or unregister user for selected conference."""
        retval = None
        prof = self._getProfileFromUser() # get user Profile

        # check if conf exists given websafeConfKey
        # get conference; check that it exists
        wsck = request.websafeConferenceKey
        conf = ndb.Key(urlsafe=wsck).get()
        if not conf:
            raise endpoints.NotFoundException(
                'No conference found with key: %s' % wsck)

        # register
        if reg:
            # check if user already registered otherwise add
            if wsck in prof.conferenceKeysToAttend:
                raise ConflictException(
                    "You have already registered for this conference")

            # check if seats avail
            if conf.seatsAvailable <= 0:
                raise ConflictException(
                    "There are no seats available.")

            # register user, take away one seat
            prof.conferenceKeysToAttend.append(wsck)
            conf.seatsAvailable -= 1
            retval = True

        # unregister
        else:
            # check if user already registered
            if wsck in prof.conferenceKeysToAttend:

                # unregister user, add back one seat
                prof.conferenceKeysToAttend.remove(wsck)
                conf.seatsAvailable += 1
                retval = True
            else:
                retval = False

        # write things back to the datastore & return
        prof.put()
        conf.put()
        return BooleanMessage(data=retval)


    @endpoints.method(message_types.VoidMessage, ConferenceForms,
            path='conferences/attending',
            http_method='GET', name='getConferencesToAttend')
    def getConferencesToAttend(self, request):
        """Get list of conferences that user has registered for."""
        prof = self._getProfileFromUser() # get user Profile
        conf_keys = [ndb.Key(urlsafe=wsck) for wsck in prof.conferenceKeysToAttend]
        conferences = ndb.get_multi(conf_keys)

        # get organizers
        organisers = [ndb.Key(Profile, conf.organizerUserId) for conf in conferences]
        profiles = ndb.get_multi(organisers)

        # put display names in a dict for easier fetching
        names = {}
        for profile in profiles:
            names[profile.key.id()] = profile.displayName

        # return set of ConferenceForm objects per Conference
        return ConferenceForms(items=[self._copyConferenceToForm(conf, names[conf.organizerUserId])\
         for conf in conferences]
        )


    @endpoints.method(CONF_GET_REQUEST, BooleanMessage,
            path='conference/{websafeConferenceKey}',
            http_method='POST', name='registerForConference')
    def registerForConference(self, request):
        """Register user for selected conference."""
        return self._conferenceRegistration(request)


    @endpoints.method(CONF_GET_REQUEST, BooleanMessage,
            path='conference/{websafeConferenceKey}',
            http_method='DELETE', name='unregisterFromConference')
    def unregisterFromConference(self, request):
        """Unregister user for selected conference."""
        return self._conferenceRegistration(request, reg=False)


    @endpoints.method(message_types.VoidMessage, SessionForms,
            path='filterPlayground',
            http_method='GET', name='filterPlayground')
    def filterPlayground(self, request):
        """Filter Playground"""
        filterTime = time(19)
        filterSessionType = "workshop"
        q = Session.query()
        # field = "city"
        # operator = "="
        # value = "London"
        # f = ndb.query.FilterNode(field, operator, value)
        # q = q.filter(f)
        q = q.filter(Session.startTime >= filterTime)
        #q = q.filter(Session.highlights=="donkey")
        #q = q.filter(session.)
        #q = q.filter(Conference.topics=="Medical Innovations")
        #q = q.filter(Conference.month==6)

        return SessionForms(
            items=[self._copySessionToForm(session) for session in q if session.typeOfSession != filterSessionType]
        )


api = endpoints.api_server([ConferenceApi]) # register API
