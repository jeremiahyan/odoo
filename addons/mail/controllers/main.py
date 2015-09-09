from operator import itemgetter
import psycopg2
import werkzeug
from werkzeug import url_encode

import openerp
from openerp import SUPERUSER_ID
from openerp import http
from openerp.exceptions import AccessError
from openerp.http import request


class MailController(http.Controller):
    _cp_path = '/mail'

    def _redirect_to_messaging(self):
        messaging_action = request.env['mail.thread']._get_inbox_action_xml_id()
        url = '/web?%s' % url_encode({'action': messaging_action})
        return werkzeug.utils.redirect(url)

    @http.route('/mail/receive', type='json', auth='none')
    def receive(self, req):
        """ End-point to receive mail from an external SMTP server. """
        dbs = req.jsonrequest.get('databases')
        for db in dbs:
            message = dbs[db].decode('base64')
            try:
                registry = openerp.registry(db)
                with registry.cursor() as cr:
                    mail_thread = registry['mail.thread']
                    mail_thread.message_process(cr, SUPERUSER_ID, None, message)
            except psycopg2.Error:
                pass
        return True

    @http.route('/mail/read_followers', type='json', auth='user')
    def read_followers(self, follower_ids):
        result = []
        is_editable = request.env.user.has_group('base.group_no_one')
        for follower in request.env['mail.followers'].browse(follower_ids):
            result.append({
                'id': follower.id,
                'name': follower.partner_id.name or follower.channel_id.name,
                'res_model': 'res.partner' if follower.partner_id else 'mail.channel',
                'res_id': follower.partner_id.id or follower.channel_id.id,
                'is_editable': is_editable,
                'is_uid': request.env.user.partner_id == follower.partner_id,
            })
        return result

    @http.route('/mail/read_subscription_data', type='json', auth='user')
    def read_subscription_data(self, res_model, res_id):
        """ Computes:
            - message_subtype_data: data about document subtypes: which are
                available, which are followed if any """
        # find the document followers, update the data
        followers = request.env['mail.followers'].search([
            ('partner_id', '=', request.env.user.partner_id.id),
            ('res_id', '=', res_id),
            ('res_model', '=', res_model),
        ])

        # find current model subtypes, add them to a dictionary
        subtypes = request.env['mail.message.subtype'].search(['&', ('hidden', '=', False), '|', ('res_model', '=', res_model), ('res_model', '=', False)])
        subtypes_list = [{
            'name': subtype.name,
            'res_model': subtype.res_model,
            'sequence': subtype.sequence,
            'default': subtype.default,
            'internal': subtype.internal,
            'followed': subtype.id in followers.mapped('subtype_ids').ids,
            'parent_model': subtype.parent_id and subtype.parent_id.res_model or False,
            'id': subtype.id
        } for subtype in subtypes]
        subtypes_list = sorted(subtypes_list, key=itemgetter('parent_model', 'res_model', 'internal', 'sequence'))

        return subtypes_list

    @http.route('/mail/view', type='http', auth='none')
    def mail_action_view(self, model=None, res_id=None, message_id=None):
        """ Generic access point from notification emails. The heuristic to
        choose where to redirect the user is the following :

         - find a public URL
         - if none found
          - users with a read access are redirected to the document
          - users without read access are redirected to the Messaging
          - not logged users are redirected to the login page
        """
        uid = request.session.uid

        if message_id:
            try:
                message = request.env['mail.message'].sudo().browse(int(message_id)).exists()
            except:
                message = request.env['mail.message']
            if message:
                model, res_id = message.model, message.res_id
            else:
                # either a wrong message_id, either someone trying ids -> just go to messaging
                return self._redirect_to_messaging()
        elif res_id and isinstance(res_id, basestring):
            res_id = int(res_id)

        # no model / res_id, meaning no possible record -> redirect to login
        if not model or not res_id or model not in request.env:
            return self._redirect_to_messaging()

        # find the access action using sudo to have the details about the access link
        RecordModel = request.env[model]
        record_sudo = RecordModel.sudo().browse(res_id).exists()
        if not record_sudo:
            # record does not seem to exist -> redirect to login
            return self._redirect_to_messaging()
        record_action = record_sudo.get_access_action()[0]

        # the record has an URL redirection: use it directly
        if record_action['type'] == 'ir.actions.act_url':
            return werkzeug.utils.redirect(record_action['url'])
        # other choice: act_window (no support of anything else currently)
        elif not record_action['type'] == 'ir.actions.act_window':
            return self._redirect_to_messaging()

        # the record has a window redirection: check access rights
        if not RecordModel.sudo(uid).check_access_rights('read', raise_exception=False):
            return self._redirect_to_messaging()
        try:
            RecordModel.sudo(uid).browse(res_id).exists().check_access_rule('read')
        except AccessError:
            return self._redirect_to_messaging()

        query = {}
        url_params = {
            'view_type': record_action['view_type'],
            'model': model,
            'id': res_id,
            'active_id': res_id,
            'view_id': record_sudo.get_formview_id(),
        }
        url = '/web?%s#%s' % (url_encode(query), url_encode(url_params))
        return werkzeug.utils.redirect(url)

    @http.route('/mail/follow', type='http', auth='user')
    def mail_action_follow(self, model, res_id):
        if model not in request.env:
            return self._redirect_to_messaging()
        Model = request.env[model]
        try:
            Model.browse(res_id).message_subscribe_users()
        except:
            return self._redirect_to_messaging()
        return werkzeug.utils.redirect('/mail/view?%s' % url_encode({'model': model, 'res_id': res_id}))

    @http.route('/mail/unfollow', type='http', auth='user')
    def mail_action_unfollow(self, model, res_id):
        if model not in request.env:
            return self._redirect_to_messaging()
        Model = request.env[model]
        try:
            Model.browse(res_id).message_unsubscribe_users()
        except:
            return self._redirect_to_messaging()
        return werkzeug.utils.redirect('/mail/view?%s' % url_encode({'model': model, 'res_id': res_id}))

    @http.route('/mail/new', type='http', auth='user')
    def mail_action_new(self, model, res_id, **kwargs):
        if model not in request.env:
            return self._redirect_to_messaging()
        params = {'view_type': 'form', 'model': model}
        if kwargs.get('view_id'):
            params['action'] = kwargs['view_id']
        return werkzeug.utils.redirect('/web?#%s' % url_encode(params))

    @http.route('/mail/method', type='http', auth='user')
    def mail_action_method(self, model, res_id, method, **kwargs):
        # only public methods / check exists
        if method.strip().startswith('_') or model not in request.env:
            return self._redirect_to_messaging()
        Model = request.env[model]
        try:
            record = Model.browse(int(res_id)).exists()
            getattr(record, method)
        except:
            return self._redirect_to_messaging()
        return werkzeug.utils.redirect('/mail/view?%s' % url_encode({'model': model, 'res_id': res_id}))

    @http.route('/mail/assign', type='http', auth='user')
    def mail_action_assign(self, model, res_id, **kwargs):
        if model not in request.env:
            return self._redirect_to_messaging()
        Model = request.env[model]
        try:
            Model.browse(int(res_id)).exists().write({'user_id': request.uid})
        except:
            return self._redirect_to_messaging()
        return werkzeug.utils.redirect('/mail/view?%s' % url_encode({'model': model, 'res_id': res_id}))

    @http.route('/mail/workflow', type='http', auth='user')
    def mail_action_workflow(self, model, res_id, signal, **kwargs):
        if model not in request.env:
            return self._redirect_to_messaging()
        Model = request.env[model]
        try:
            Model.browse(int(res_id)).exists().signal_workflow(signal)
        except:
            return self._redirect_to_messaging()
        return werkzeug.utils.redirect('/mail/view?%s' % url_encode({'model': model, 'res_id': res_id}))

    @http.route('/mail/needaction', type='json', auth='user')
    def needaction(self):
        return request.env['res.partner'].get_needaction_count()

    @http.route('/mail/client_action', type='json', auth='user')
    def mail_client_action(self):
        values = {
            'needaction_inbox_counter': request.env['res.partner'].get_needaction_count(),
            'chatter_needaction_auto': request.env.user.chatter_needaction_auto,
            'channel_slots': request.env['mail.channel'].channel_fetch_slot()
        }
        return values
